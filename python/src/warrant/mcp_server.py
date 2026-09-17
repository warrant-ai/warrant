"""An MCP server that lets any MCP-capable agent query its mandate and record decisions.

    warrant mcp --stream lending --policy policies/ --agent-name credit-underwriter --agent-version 2.4.0

Four tools: ``describe_mandate`` and ``check_mandate`` before the action, ``record_decision``
after it, ``record_outcome`` when the result is known.

What the agent cannot do is as important as what it can. The agent's identity comes from
the server's configuration, never from a tool argument. The mandate on a record is always
evaluated here, from the inputs, and never supplied by the agent: an agent that reports it
acted where the policy said no is recorded exactly that way and queues for human review.
There is no tool for recording a human verdict. Requires ``pip install "warrantai[mcp]"``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from warrant.client import EVIDENCE_TYPES, Verdict, Warrant
from warrant.schema import ValidationError

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover
    raise ImportError('the MCP server needs the mcp package, version 2 or newer: pip install "warrantai[mcp]"') from exc

log = logging.getLogger("warrant.mcp")

MAX_EVIDENCE = 100
MAX_MODEL_CALLS = 100
MAX_INPUT_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 1024 * 1024
_CLASS_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

INSTRUCTIONS = (
    "Warrant keeps the record of what you decide. Before a consequential action, call check_mandate with the "
    "decision class and the facts you are deciding on. Act only if it returns allowed: true; if it returns escalate, "
    "hand the case to a human. Afterwards call record_decision with the same inputs, the evidence you relied on, and "
    "the action you took (leave action out if you did not act). When the result of the decision is known, call "
    "record_outcome. describe_mandate shows the written policy for a decision class."
)


def _verdict(verdict: Verdict) -> Dict[str, Any]:
    out: Dict[str, Any] = {"result": verdict.result, "allowed": verdict.allowed}
    for key in ("policy_id", "policy_version", "clause", "reason"):
        value = getattr(verdict, key)
        if value:
            out[key] = value
    if verdict.flagged:
        out["flagged"] = True
    return out


# Every argument is checked before a decision scope opens: a scope that raises records a
# failed decision, and a malformed tool call is not a decision.


def _text(value: Any, label: str, *, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{label} must be a non-empty string")
    return value


def _amount(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ToolError(f"{label} must be a number of zero or more")
    return float(value)


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError(f"{label} must be a whole number of zero or more")
    return value


def _decision_class(value: Any) -> str:
    if not isinstance(value, str) or not _CLASS_RE.match(value):
        raise ToolError(f"decision_class must look like 'credit.approve', got {value!r}")
    return value


def _inputs(inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    inputs = inputs or {}
    if not isinstance(inputs, dict) or not all(isinstance(k, str) for k in inputs):
        raise ToolError("inputs must be an object with string keys")
    if "self" in inputs:
        raise ToolError("'self' cannot be used as an input name")
    size = len(json.dumps(inputs))
    if size > MAX_INPUT_BYTES:
        raise ToolError(f"inputs are {size} bytes; the limit is {MAX_INPUT_BYTES}")
    return inputs


def _evidence(items: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    items = items or []
    if len(items) > MAX_EVIDENCE:
        raise ToolError(f"at most {MAX_EVIDENCE} evidence items per decision")
    out = []
    for i, item in enumerate(items):
        label = f"evidence[{i}]"
        if not isinstance(item, dict):
            raise ToolError(f"{label} must be an object")
        kind = item.get("type", "other")
        if kind not in EVIDENCE_TYPES:
            raise ToolError(f"{label}.type must be one of {', '.join(EVIDENCE_TYPES)}")
        content, digest = item.get("content"), item.get("content_hash")
        if content is None and digest is None:
            raise ToolError(f"{label} needs content (hashed here, never stored) or content_hash")
        if digest is not None and (not isinstance(digest, str) or not _SHA256_RE.match(digest)):
            raise ToolError(f"{label}.content_hash must be 64 lowercase hex characters (sha256)")
        if content is not None and len(content.encode("utf-8") if isinstance(content, str) else json.dumps(content)) > MAX_CONTENT_BYTES:
            raise ToolError(f"{label}.content is over {MAX_CONTENT_BYTES} bytes; pass content_hash instead")
        out.append({"name": _text(item.get("name"), f"{label}.name"), "uri": _text(item.get("uri"), f"{label}.uri"), "type": kind,
                    "content": content, "content_hash": digest, "excerpt": _text(item.get("excerpt"), f"{label}.excerpt", required=False)})
    return out


def _model_calls(calls: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    calls = calls or []
    if len(calls) > MAX_MODEL_CALLS:
        raise ToolError(f"at most {MAX_MODEL_CALLS} model calls per decision")
    out = []
    for i, call in enumerate(calls):
        label = f"model_calls[{i}]"
        if not isinstance(call, dict):
            raise ToolError(f"{label} must be an object")
        out.append({"provider": _text(call.get("provider"), f"{label}.provider"), "model": _text(call.get("model"), f"{label}.model"),
                    "tokens_in": _count(call.get("tokens_in", 0), f"{label}.tokens_in"), "tokens_out": _count(call.get("tokens_out", 0), f"{label}.tokens_out"),
                    "amount": _amount(call.get("amount", 0.0), f"{label}.amount")})
    return out


def create_server(client: Warrant) -> MCPServer:
    """Build the MCP server around a configured ``Warrant`` client. The caller owns the client."""
    server = MCPServer("warrant", instructions=INSTRUCTIONS)

    @server.tool(structured_output=False)
    def describe_mandate(decision_class: str) -> str:
        """The written policy that governs a decision class: its clauses in order, what happens
        when none matches, and what happens when a clause cannot be evaluated. Returns JSON."""
        decision_class = _decision_class(decision_class)
        bundle = getattr(client.policy, "bundle", None)
        policy = bundle.policy_for(decision_class) if bundle is not None else None
        if policy is None:
            return json.dumps({"decision_class": decision_class, "governed": False, "detail": "no policy governs this class; decisions are recorded as unchecked"})
        return json.dumps({
            "decision_class": decision_class, "governed": True, "policy_id": policy.policy_id, "policy_version": policy.version,
            "title": policy.title, "default": policy.default, "fail_mode": policy.fail_mode,
            "clauses": [{"id": c.id, "title": c.title, "when": c.when, "result": c.result} for c in policy.clauses],
        }, ensure_ascii=False)

    @server.tool(structured_output=False)
    def check_mandate(decision_class: str, inputs: Optional[Dict[str, Any]] = None) -> str:
        """Ask whether an action is within mandate, before taking it. Nothing is recorded.
        Returns JSON with result (allow, deny, escalate or unchecked) and allowed, which is true for allow only."""
        decision_class, inputs = _decision_class(decision_class), _inputs(inputs)
        if client.policy is None:
            return json.dumps(_verdict(Verdict("unchecked", reason="no policy engine configured")))
        return json.dumps(_verdict(client.policy.evaluate(decision_class, inputs)), ensure_ascii=False)

    @server.tool(structured_output=False)
    def record_decision(
        decision_class: str,
        subject: str,
        inputs: Optional[Dict[str, Any]] = None,
        action: Optional[str] = None,
        summary: Optional[str] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        model_calls: Optional[List[Dict[str, Any]]] = None,
        cost: Optional[float] = None,
        cost_centre: Optional[str] = None,
        require_human: bool = False,
        human_note: Optional[str] = None,
        on_behalf_of: Optional[str] = None,
        alternatives: Optional[List[str]] = None,
    ) -> str:
        """Record one decision. Pass action if you took it; leave it out if you held back.
        The mandate is evaluated here from inputs and written on the record; you cannot supply it.
        evidence items: {name, uri, type?, content? or content_hash?, excerpt?}; content is hashed here and never stored.
        model_calls items: {provider, model, tokens_in?, tokens_out?, amount?}. cost adds any other cost.
        Returns JSON with the record_id to pass to record_outcome later."""
        decision_class, subject, inputs = _decision_class(decision_class), _text(subject, "subject"), _inputs(inputs)
        items, calls = _evidence(evidence), _model_calls(model_calls)
        action = _text(action, "action", required=False)
        other_cost = _amount(cost, "cost") if cost is not None else None
        if alternatives is not None and not all(isinstance(a, str) for a in alternatives):
            raise ToolError("alternatives must be a list of strings")
        try:
            with client.decide(decision_class, subject=subject, on_behalf_of=on_behalf_of, alternatives=alternatives) as d:
                verdict = d.check(**inputs)
                for item in items:
                    d.evidence(item["name"], uri=item["uri"], type=item["type"], content=item["content"], content_hash=item["content_hash"], excerpt=item["excerpt"])
                for call in calls:
                    d.model_call(call["provider"], call["model"], tokens_in=call["tokens_in"], tokens_out=call["tokens_out"], amount=call["amount"])
                if other_cost is not None:
                    d.cost(other_cost)
                if require_human:
                    d.require_human(note=human_note)
                if action:
                    d.act(action, summary=summary, cost_centre=cost_centre)
        except ValidationError as exc:
            raise ToolError("the decision could not be recorded: " + "; ".join(exc.errors[:3])) from exc
        outside = bool(action) and verdict.result in ("deny", "escalate")
        log.info("warrant mcp recorded decision %s class=%s status=%s mandate=%s", d.record_id, decision_class, "acted" if action else "withheld", verdict.result)
        result: Dict[str, Any] = {"record_id": d.record_id, "status": "acted" if action else "withheld", "mandate": _verdict(verdict), "outside_mandate": outside}
        if outside:
            result["detail"] = f"Recorded. The policy result was {verdict.result}, so this action is outside the mandate and will be queued for human review."
        return json.dumps(result, ensure_ascii=False)

    @server.tool(structured_output=False)
    def record_outcome(label: str, decision_record_id: Optional[str] = None, subject: Optional[str] = None,
                       score: Optional[float] = None, source: Optional[str] = None, observed_at: Optional[str] = None) -> str:
        """Attach how a past decision turned out, for example performing or default.
        Identify the decision by the record_id that record_decision returned, or by subject."""
        try:
            record_id = client.outcome(label=_text(label, "label"), decision_record_id=decision_record_id, subject=subject, score=score, source=source, observed_at=observed_at)
        except (LookupError, ValueError, TypeError, ValidationError) as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps({"record_id": record_id, "label": label})

    return server


def serve(client: Warrant) -> None:
    """Run over stdio until the client disconnects, then deliver what is still queued."""
    server = create_server(client)
    log.info("warrant mcp serving stream %s as %s@%s over stdio", client.stream, client.agent.name, client.agent.version)
    try:
        server.run("stdio")
    finally:
        client.close()
