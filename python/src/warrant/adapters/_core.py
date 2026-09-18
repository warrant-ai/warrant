"""What every adapter shares: reading a decision out of a tool call, the mandate check,
recording, and what the model is told when the mandate says no."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from warrant.client import Verdict, Warrant

log = logging.getLogger("warrant.adapters")

Pricer = Callable[[str, str, int, int], float]
"""``(provider, model, tokens_in, tokens_out) -> amount`` in the client's currency."""

MAX_EVIDENCE = 100


class AdapterError(ValueError):
    """A tool call could not be read as the decision it is mapped to."""


class ToolCallFailed(Exception):
    """Raised inside a decision scope so a failed tool call is recorded as a failed decision."""


@dataclass(frozen=True)
class ToolDecision:
    """How one tool's calls map to decisions.

    ``subject`` is the name of the tool argument that identifies what is being decided on,
    or a function of the arguments. ``inputs`` are the facts the policy is checked against:
    argument names, a function of the arguments, or ``None`` for every argument.
    ``action`` defaults to the tool's name.
    """

    decision_class: str
    subject: Union[str, Callable[[Mapping[str, Any]], str]]
    action: Optional[str] = None
    inputs: Union[Sequence[str], Callable[[Mapping[str, Any]], Mapping[str, Any]], None] = None
    cost_centre: Optional[str] = None

    def read(self, tool_name: str, args: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Return ``(subject, inputs)`` for one call, or raise ``AdapterError``."""
        if not isinstance(args, Mapping):
            raise AdapterError(f"{tool_name}: tool arguments are not an object")
        try:
            subject = self.subject(args) if callable(self.subject) else args.get(self.subject)
            if callable(self.inputs):
                inputs = dict(self.inputs(args))
            elif self.inputs is None:
                inputs = dict(args)
            else:
                inputs = {key: args[key] for key in self.inputs if key in args}
        except Exception as exc:  # user-supplied mapping functions can raise anything
            raise AdapterError(f"{tool_name}: could not read the decision from the tool arguments: {type(exc).__name__}") from exc
        if isinstance(subject, (int, float)) and not isinstance(subject, bool):
            subject = str(subject)
        if not isinstance(subject, str) or not subject:
            name = getattr(self.subject, "__name__", self.subject)
            raise AdapterError(f"{tool_name}: no subject; expected a non-empty {name!r} in the tool arguments")
        if "self" in inputs:
            raise AdapterError(f"{tool_name}: 'self' cannot be used as an input name")
        try:
            inputs = json.loads(json.dumps(inputs))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{tool_name}: policy inputs must be JSON values") from exc
        return subject, inputs


def evaluate(client: Warrant, decision_class: str, inputs: Mapping[str, Any]) -> Verdict:
    if client.policy is None:
        return Verdict("unchecked", reason="no policy engine configured")
    return client.policy.evaluate(decision_class, inputs)


def blocked_message(tool_name: str, verdict: Verdict) -> str:
    """What the model reads when its tool call is not carried out."""
    where = f"policy {verdict.policy_id}" + (f" clause {verdict.clause}" if verdict.clause else "") if verdict.policy_id else "the policy"
    why = f" ({verdict.reason})" if verdict.reason else ""
    if verdict.result == "escalate":
        return f"{tool_name} was not carried out: {where} requires a human to decide this{why}. Do not retry. Hand the case to a human reviewer and say why."
    return f"{tool_name} was not carried out: {where} does not allow it{why}. Do not retry with changed arguments. Tell the user what the policy says."


def as_evidence_content(value: Any) -> Any:
    """Something ``evidence(content=...)`` can hash: JSON as is, anything else by its text."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return str(value)


@dataclass
class EvidenceItem:
    name: str
    uri: str
    content_hash: str
    type: str = "tool_call"


@dataclass
class ModelUsage:
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


def record(
    client: Warrant,
    mapping: ToolDecision,
    tool_name: str,
    subject: str,
    inputs: Mapping[str, Any],
    *,
    status: str,
    evidence: Sequence[EvidenceItem] = (),
    result: Any = None,
    usage: Sequence[ModelUsage] = (),
    pricer: Optional[Pricer] = None,
    human_required: bool = False,
    human_note: Optional[str] = None,
    record_id: Optional[str] = None,
) -> Tuple[str, Verdict]:
    """Write one decision record. ``status`` is ``acted``, ``withheld`` or ``failed``.

    The mandate is evaluated here, on the same inputs the gate saw, so the record carries
    what the policy said and not what the adapter remembers. ``record_id`` is for adapters
    whose runtime may deliver the same attempt twice; see ``Warrant.decide``.
    """
    if status not in ("acted", "withheld", "failed"):
        raise ValueError(f"status must be acted, withheld or failed, got {status!r}")
    verdict: Optional[Verdict] = None
    try:
        with client.decide(mapping.decision_class, subject=subject, record_id=record_id) as d:
            record_id = d.record_id
            verdict = d.check(**inputs)
            for item in list(evidence)[-MAX_EVIDENCE:]:
                d.evidence(item.name, uri=item.uri, type=item.type, content_hash=item.content_hash)
            if result is not None:
                d.evidence(f"{tool_name}.result", uri=f"tool://{tool_name}", type="tool_call", content=as_evidence_content(result))
            for call in usage:
                amount = 0.0
                if pricer is not None:
                    try:
                        amount = float(pricer(call.provider, call.model, call.tokens_in, call.tokens_out))
                    except Exception as exc:  # a pricing bug must not lose the record
                        log.warning("warrant: pricer failed for %s/%s, recording cost 0: %s", call.provider, call.model, type(exc).__name__)
                d.model_call(call.provider, call.model, tokens_in=call.tokens_in, tokens_out=call.tokens_out, amount=amount)
            if human_required or verdict.result == "escalate":
                d.require_human(note=human_note)
            if status == "acted":
                d.act(mapping.action or tool_name, cost_centre=mapping.cost_centre)
            elif status == "failed":
                raise ToolCallFailed()
    except ToolCallFailed:
        pass
    assert verdict is not None
    log.info("warrant adapter recorded %s class=%s status=%s mandate=%s", record_id, mapping.decision_class, status, verdict.result)
    return record_id, verdict


class EvidenceLog:
    """Tool results seen in one session, kept as hashes only, waiting for the next decision.

    ``max_sessions`` bounds the number of sessions remembered, for runtimes where a session
    can end without a decision and nothing tells the adapter; the oldest are forgotten first.
    """

    def __init__(self, max_sessions: Optional[int] = None) -> None:
        self._by_session: Dict[str, List[EvidenceItem]] = {}
        self._max_sessions = max_sessions

    def add(self, session: str, tool_name: str, call_id: str, result: Any) -> None:
        from warrant.hashing import content_hash

        items = self._by_session.setdefault(session, [])
        items.append(EvidenceItem(tool_name, f"tool://{tool_name}#{call_id}", content_hash(as_evidence_content(result))))
        del items[:-MAX_EVIDENCE]
        while self._max_sessions is not None and len(self._by_session) > self._max_sessions:
            del self._by_session[next(iter(self._by_session))]

    def peek(self, session: str) -> List[EvidenceItem]:
        """The session's evidence without clearing it: for a failed attempt the next one may still need it."""
        return list(self._by_session.get(session, []))

    def take(self, session: str) -> List[EvidenceItem]:
        return self._by_session.pop(session, [])
