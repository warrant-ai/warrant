"""Claude Agent SDK adapter: hooks that gate and record the tool calls you name as decisions.

    from claude_agent_sdk import ClaudeAgentOptions, query
    from warrant.adapters import ToolDecision
    from warrant.adapters.claude_agent import WarrantHooks

    guard = WarrantHooks(w, {"mcp__bank__approve_loan": ToolDecision("credit.approve", subject="loan_id", action="approve")})
    options = ClaudeAgentOptions(hooks=guard.hooks(), ...)
    async for message in query(prompt=..., options=options):
        guard.observe(message)          # optional: attributes model usage to the next decision

Before a mapped tool runs, its arguments are checked against the policy. ``deny`` blocks
the call and tells the model why. ``escalate`` blocks it too (``on_escalate="deny"``, the
default, for unattended agents) or hands it to the host's own permission prompt
(``on_escalate="ask"``). ``allow`` and ``unchecked`` change nothing: the adapter only ever
restricts, it never approves a call the host would have asked about. After the tool runs
the decision is recorded, with the other tool results seen since the last decision attached
as evidence, by hash. Requires ``pip install "warrantai[claude-agent]"``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

from warrant.adapters._core import AdapterError, EvidenceLog, ModelUsage, Pricer, ToolDecision, blocked_message, evaluate, record
from warrant.client import Warrant

try:
    from claude_agent_sdk import AssistantMessage, HookMatcher
except ImportError as exc:  # pragma: no cover
    raise ImportError('the Claude Agent SDK adapter needs claude-agent-sdk: pip install "warrantai[claude-agent]"') from exc

log = logging.getLogger("warrant.adapters.claude_agent")


def _business_tool(tool_name: str) -> bool:
    """Custom and MCP tools, not the host's file and shell tools, which would flood a record."""
    return tool_name.startswith("mcp__")


class WarrantHooks:
    def __init__(
        self,
        client: Warrant,
        decisions: Mapping[str, ToolDecision],
        *,
        on_escalate: str = "deny",
        evidence: Callable[[str], bool] = _business_tool,
        pricer: Optional[Pricer] = None,
    ) -> None:
        if on_escalate not in ("deny", "ask"):
            raise ValueError(f"on_escalate must be 'deny' or 'ask', got {on_escalate!r}")
        if not decisions:
            raise ValueError("decisions is empty: name at least one tool whose calls are decisions")
        self._client = client
        self._decisions = dict(decisions)
        self._on_escalate = on_escalate
        self._is_evidence = evidence
        self._pricer = pricer
        self._evidence = EvidenceLog()
        self._usage: Dict[str, List[ModelUsage]] = {}
        # Calls handed to the host's permission prompt: recorded when they run, or as withheld when the session stops.
        self._asked: Dict[str, Dict[str, Any]] = {}

    def hooks(self, existing: Optional[Mapping[str, List[Any]]] = None) -> Dict[str, List[Any]]:
        """The ``hooks`` value for ``ClaudeAgentOptions``, added after any hooks you already have."""
        merged: Dict[str, List[Any]] = {event: list(matchers) for event, matchers in (existing or {}).items()}
        for event, callback in (("PreToolUse", self._pre), ("PostToolUse", self._post), ("PostToolUseFailure", self._failure), ("Stop", self._stop)):
            merged.setdefault(event, []).append(HookMatcher(hooks=[callback]))
        return merged

    def observe(self, message: Any) -> None:
        """Note an assistant message's token usage so it lands on the next decision in that session."""
        if not isinstance(message, AssistantMessage) or not message.usage or not message.session_id:
            return
        usage = message.usage
        self._usage.setdefault(message.session_id, []).append(
            ModelUsage("anthropic", message.model, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
        )

    # -- hooks -------------------------------------------------------------------

    async def _pre(self, data: Mapping[str, Any], tool_use_id: Optional[str], context: Any) -> Dict[str, Any]:
        tool_name = data["tool_name"]
        mapping = self._decisions.get(tool_name)
        if mapping is None:
            return {}
        try:
            subject, inputs = mapping.read(tool_name, data["tool_input"])
        except AdapterError as exc:
            # The gate cannot tell what is being decided, so the call does not go ahead.
            log.error("warrant adapter blocked %s: %s", tool_name, exc)
            return _deny(f"{tool_name} was not carried out: Warrant could not read the decision from this call ({exc}).")
        verdict = evaluate(self._client, mapping.decision_class, inputs)
        if verdict.result in ("allow", "unchecked"):
            return {}
        if verdict.result == "escalate" and self._on_escalate == "ask":
            self._asked[data["tool_use_id"]] = {"session": data["session_id"], "tool_name": tool_name, "subject": subject, "inputs": inputs}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": blocked_message(tool_name, verdict)}}
        self._record(data["session_id"], tool_name, subject, inputs, status="withheld")
        return _deny(blocked_message(tool_name, verdict))

    async def _post(self, data: Mapping[str, Any], tool_use_id: Optional[str], context: Any) -> Dict[str, Any]:
        tool_name, session = data["tool_name"], data["session_id"]
        mapping = self._decisions.get(tool_name)
        if mapping is None:
            if self._is_evidence(tool_name):
                self._evidence.add(session, tool_name, data["tool_use_id"], data.get("tool_response"))
            return {}
        asked = self._asked.pop(data["tool_use_id"], None)
        try:
            subject, inputs = (asked["subject"], asked["inputs"]) if asked else mapping.read(tool_name, data["tool_input"])
        except AdapterError as exc:  # the gate would have blocked this; reaching here means the arguments changed after it
            log.error("warrant adapter could not record %s: %s", tool_name, exc)
            return {}
        self._record(session, tool_name, subject, inputs, status="acted", result=data.get("tool_response"),
                     human_note="approved in the host's permission prompt" if asked else None)
        return {}

    async def _failure(self, data: Mapping[str, Any], tool_use_id: Optional[str], context: Any) -> Dict[str, Any]:
        tool_name = data["tool_name"]
        mapping = self._decisions.get(tool_name)
        if mapping is None:
            return {}
        self._asked.pop(data["tool_use_id"], None)
        try:
            subject, inputs = mapping.read(tool_name, data["tool_input"])
        except AdapterError:
            return {}
        # The host's error text is not recorded: it can carry customer data.
        self._record(data["session_id"], tool_name, subject, inputs, status="failed")
        return {}

    async def _stop(self, data: Mapping[str, Any], tool_use_id: Optional[str], context: Any) -> Dict[str, Any]:
        session = data["session_id"]
        for call_id in [k for k, v in self._asked.items() if v["session"] == session]:
            asked = self._asked.pop(call_id)
            self._record(session, asked["tool_name"], asked["subject"], asked["inputs"], status="withheld", human_note="not approved in the host's permission prompt")
        self._evidence.take(session)
        self._usage.pop(session, None)
        return {}

    def _record(self, session: str, tool_name: str, subject: str, inputs: Mapping[str, Any], *, status: str, result: Any = None, human_note: Optional[str] = None) -> None:
        record(self._client, self._decisions[tool_name], tool_name, subject, inputs, status=status, evidence=self._evidence.take(session),
               result=result, usage=self._usage.pop(session, []), pricer=self._pricer, human_note=human_note)


def _deny(reason: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
