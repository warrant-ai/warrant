"""LangGraph adapter: a ``ToolNode`` wrapper that gates and records the tool calls you name as decisions.

    from langgraph.prebuilt import ToolNode
    from warrant.adapters import ToolDecision
    from warrant.adapters.langgraph import WarrantToolGuard

    guard = WarrantToolGuard(w, {"approve_loan": ToolDecision("credit.approve", subject="loan_id", action="approve")})
    tools = ToolNode([approve_loan, bureau_pull], wrap_tool_call=guard.wrap, awrap_tool_call=guard.awrap)

Before a mapped tool runs, its arguments are checked against the policy. ``deny`` and, by
default, ``escalate`` return an error ``ToolMessage`` that tells the model why, and the tool
does not run. ``on_escalate="interrupt"`` pauses the graph with LangGraph's ``interrupt()``
instead; resume with ``Command(resume={"approve": True, "reviewer": "asha"})`` to let the
call through. Every outcome is recorded, and the other tool results seen on the same thread
since the last decision are attached as evidence, by hash. Evidence is only collected when
the graph runs with a ``thread_id``, so one customer's lookups can never land on another's
decision. Requires ``pip install "warrantai[langgraph]"``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

from warrant.adapters._core import AdapterError, EvidenceLog, ToolDecision, blocked_message, evaluate, record
from warrant.client import Verdict, Warrant

try:
    from langchain_core.messages import ToolMessage
    from langgraph.errors import GraphBubbleUp
    from langgraph.types import interrupt
except ImportError as exc:  # pragma: no cover
    raise ImportError('the LangGraph adapter needs langgraph: pip install "warrantai[langgraph]"') from exc

log = logging.getLogger("warrant.adapters.langgraph")


def _default_evidence(tool_name: str) -> bool:
    return True


class WarrantToolGuard:
    def __init__(self, client: Warrant, decisions: Mapping[str, ToolDecision], *, on_escalate: str = "block",
                 evidence: Callable[[str], bool] = _default_evidence) -> None:
        if on_escalate not in ("block", "interrupt"):
            raise ValueError(f"on_escalate must be 'block' or 'interrupt', got {on_escalate!r}")
        if not decisions:
            raise ValueError("decisions is empty: name at least one tool whose calls are decisions")
        self._client = client
        self._decisions = dict(decisions)
        self._on_escalate = on_escalate
        self._is_evidence = evidence
        self._evidence = EvidenceLog()

    def wrap(self, request: Any, execute: Callable[[Any], Any]) -> Any:
        """``wrap_tool_call`` for ``ToolNode``."""
        gate = self._gate(request)
        if gate is None:
            return self._after_other(request, execute(request))
        blocked, state = gate
        if blocked is not None:
            return blocked
        try:
            result = execute(request)
        except GraphBubbleUp:
            raise  # the graph pausing or routing, not the tool failing; the node runs again on resume
        except Exception:
            self._failed(request, state)
            raise
        return self._after_decision(request, state, result)

    async def awrap(self, request: Any, execute: Callable[[Any], Awaitable[Any]]) -> Any:
        """``awrap_tool_call`` for ``ToolNode``."""
        gate = self._gate(request)
        if gate is None:
            return self._after_other(request, await execute(request))
        blocked, state = gate
        if blocked is not None:
            return blocked
        try:
            result = await execute(request)
        except GraphBubbleUp:
            raise
        except Exception:
            self._failed(request, state)
            raise
        return self._after_decision(request, state, result)

    # -- steps ---------------------------------------------------------------------

    def _gate(self, request: Any) -> Optional[Tuple[Optional[ToolMessage], Any]]:
        """``None`` for a tool that is not a decision; otherwise a blocking message, or the state to record with.

        Nothing is recorded before ``interrupt()``: it raises to pause the graph, and the node runs again on resume.
        """
        call = request.tool_call
        tool_name = call["name"]
        mapping = self._decisions.get(tool_name)
        if mapping is None:
            return None
        try:
            subject, inputs = mapping.read(tool_name, call.get("args") or {})
        except AdapterError as exc:
            log.error("warrant adapter blocked %s: %s", tool_name, exc)
            return _refusal(call, f"{tool_name} was not carried out: Warrant could not read the decision from this call ({exc})."), None
        verdict = evaluate(self._client, mapping.decision_class, inputs)
        thread = _thread(request)
        review: Optional[Tuple[bool, Optional[str]]] = None
        if verdict.result == "deny" or (verdict.result == "escalate" and self._on_escalate == "block"):
            self._record(thread, tool_name, subject, inputs, status="withheld")
            return _refusal(call, blocked_message(tool_name, verdict)), None
        if verdict.result == "escalate":
            answer = interrupt({"warrant": "escalate", "tool": tool_name, "subject": subject, "decision_class": mapping.decision_class,
                                "policy_id": verdict.policy_id, "clause": verdict.clause, "reason": verdict.reason, "args": call.get("args") or {}})
            approved = bool(answer.get("approve")) if isinstance(answer, Mapping) else answer is True
            reviewer = answer.get("reviewer") if isinstance(answer, Mapping) else None
            review = (approved, reviewer if isinstance(reviewer, str) and reviewer else None)
            if not approved:
                self._record(thread, tool_name, subject, inputs, status="withheld", review=review)
                return _refusal(call, f"{tool_name} was not carried out: a human reviewer did not approve it. Do not retry."), None
        return None, (thread, subject, inputs, review)

    def _after_decision(self, request: Any, state: Any, result: Any) -> Any:
        thread, subject, inputs, review = state
        failed = isinstance(result, ToolMessage) and result.status == "error"
        # A failed tool's message is not recorded: it can carry customer data.
        self._record(thread, request.tool_call["name"], subject, inputs, status="failed" if failed else "acted",
                     result=None if failed else _content(result), review=review)
        return result

    def _failed(self, request: Any, state: Any) -> None:
        thread, subject, inputs, review = state
        self._record(thread, request.tool_call["name"], subject, inputs, status="failed", review=review)

    def _after_other(self, request: Any, result: Any) -> Any:
        thread, call = _thread(request), request.tool_call
        if thread and self._is_evidence(call["name"]) and not (isinstance(result, ToolMessage) and result.status == "error"):
            self._evidence.add(thread, call["name"], str(call.get("id") or ""), _content(result))
        return result

    def _record(self, thread: Optional[str], tool_name: str, subject: str, inputs: Mapping[str, Any], *, status: str,
                result: Any = None, review: Optional[Tuple[bool, Optional[str]]] = None) -> Verdict:
        evidence = self._evidence.take(thread) if thread else []
        note = None if review is None else ("approved" if review[0] else "not approved") + " at the graph interrupt"
        record_id, verdict = record(self._client, self._decisions[tool_name], tool_name, subject, inputs, status=status, evidence=evidence, result=result, human_note=note)
        if review is not None and review[1]:
            # The reviewer's name comes from the application's resume payload, not from the model.
            self._client.human_verdict(reviewer=review[1], verdict="approve" if review[0] else "reject", decision_record_id=record_id, note=note)
        return verdict


def _thread(request: Any) -> Optional[str]:
    config = getattr(getattr(request, "runtime", None), "config", None) or {}
    thread = (config.get("configurable") or {}).get("thread_id") if isinstance(config, Mapping) else None
    return str(thread) if thread else None


def _content(result: Any) -> Any:
    return result.content if isinstance(result, ToolMessage) else str(result)


def _refusal(call: Mapping[str, Any], text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call.get("id") or "", name=call["name"], status="error")
