"""The Warrant client and the ``decide()`` context manager."""

from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Type, Union

from warrant.emit import Emitter, Sink
from warrant.hashing import content_hash
from warrant.ids import ulid
from warrant.redaction import Redactor
from warrant.schema import SCHEMA_VERSION, ValidationError, validate
from warrant.store import SQLiteStore

log = logging.getLogger("warrant")

_CLASS_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_TYPES = ("model_call", "tool_call", "document", "web", "other")
MANDATE_RESULTS = ("allow", "deny", "escalate", "unchecked")
HUMAN_VERDICTS = ("approve", "reject", "amend")

_current: "contextvars.ContextVar[Optional[Decision]]" = contextvars.ContextVar("warrant_decision", default=None)


def current_decision() -> Optional["Decision"]:
    """The innermost open decision in this context, for integrations that attach evidence."""
    return _current.get()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _as_timestamp(value: Union[str, datetime, None]) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"not an ISO 8601 timestamp: {value!r}") from exc
        return value
    raise TypeError("timestamp must be an ISO 8601 string or an aware datetime")


@dataclass(frozen=True)
class Verdict:
    """Result of a mandate check."""

    result: str
    policy_id: Optional[str] = None
    policy_version: Optional[str] = None
    clause: Optional[str] = None
    reason: Optional[str] = None
    flagged: bool = False

    def __post_init__(self) -> None:
        if self.result not in MANDATE_RESULTS:
            raise ValueError(f"mandate result must be one of {MANDATE_RESULTS}, got {self.result!r}")

    @property
    def allowed(self) -> bool:
        """True for ``allow``. ``unchecked`` is not allowed: a policy engine has to say so."""
        return self.result == "allow"


class PolicyEngine(Protocol):
    def evaluate(self, decision_class: str, inputs: Mapping[str, Any]) -> Verdict:
        """Evaluate the mandate for one decision class against the supplied inputs."""


@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    instance: Optional[str] = None


class Decision:
    """One consequential action, opened by ``Warrant.decide()`` and closed on scope exit.

    Not thread-safe: use one Decision per decision scope, in the thread that opened it.
    """

    def __init__(
        self,
        client: "Warrant",
        decision_class: str,
        subject: str,
        *,
        on_behalf_of: Optional[str],
        alternatives: Optional[Sequence[str]],
    ) -> None:
        if not _CLASS_RE.match(decision_class):
            raise ValueError(f"decision class must look like 'credit.approve', got {decision_class!r}")
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        self._client = client
        self.record_id = ulid()
        self.decision_class = decision_class
        self.subject = subject
        self._on_behalf_of = on_behalf_of
        self._alternatives = list(alternatives) if alternatives else []
        self._opened_at = _now()
        self._acted_at: Optional[str] = None
        self._action: Optional[str] = None
        self._summary: Optional[str] = None
        self._cost_centre: Optional[str] = None
        self._verdict: Optional[Verdict] = None
        self._evidence: List[Dict[str, Any]] = []
        self._cost_items: List[Dict[str, Any]] = []
        self._human: Dict[str, Any] = {"required": False}
        self._token: Optional[contextvars.Token] = None
        self._closed = False

    # -- mandate -------------------------------------------------------------

    def check(self, **inputs: Any) -> Verdict:
        """Ask the policy engine whether this action is within mandate. Synchronous; runs locally."""
        self._assert_open()
        engine = self._client.policy
        if engine is None:
            self._verdict = Verdict("unchecked", reason="no policy engine configured")
        else:
            self._verdict = engine.evaluate(self.decision_class, inputs)
        return self._verdict

    # -- evidence and cost ---------------------------------------------------

    def evidence(
        self,
        name: str,
        *,
        uri: str,
        type: str = "other",
        content: Any = None,
        content_hash: Optional[str] = None,
        excerpt: Optional[str] = None,
        retrieved_at: Union[str, datetime, None] = None,
    ) -> str:
        """Attach evidence by reference. Content is hashed here and never stored. Returns the hash."""
        self._assert_open()
        if not isinstance(name, str) or not name:
            raise ValueError("evidence name must be a non-empty string")
        if not isinstance(uri, str) or not uri:
            raise ValueError("evidence uri must be a non-empty string")
        if type not in EVIDENCE_TYPES:
            raise ValueError(f"evidence type must be one of {EVIDENCE_TYPES}, got {type!r}")
        if content is None and content_hash is None:
            raise ValueError("evidence needs either content (hashed locally) or content_hash")
        if content_hash is not None and not _SHA256_RE.match(content_hash):
            raise ValueError("content_hash must be 64 lowercase hex characters (sha256)")
        digest = content_hash if content_hash is not None else _hash_content(content)
        item: Dict[str, Any] = {"name": name, "type": type, "uri": uri, "content_hash": digest}
        if retrieved_at is not None:
            item["retrieved_at"] = _as_timestamp(retrieved_at)
        if excerpt is not None:
            if not isinstance(excerpt, str):
                raise TypeError("excerpt must be a string")
            item["excerpt"] = excerpt
        self._evidence.append(item)
        return digest

    def model_call(
        self,
        provider: str,
        model: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        amount: float = 0.0,
        uri: Optional[str] = None,
        content: Any = None,
        content_hash: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> str:
        """Record one model call as evidence and as a cost line."""
        self._assert_open()
        digest = self.evidence(
            f"{provider}/{model}",
            uri=uri or f"model://{provider}/{model}",
            type="model_call",
            content=content if content is not None or content_hash is not None else {"provider": provider, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out},
            content_hash=content_hash,
            excerpt=excerpt,
        )
        self.cost(amount, kind="model_call", provider=provider, model=model, tokens_in=tokens_in, tokens_out=tokens_out)
        return digest

    def tool_call(
        self,
        name: str,
        *,
        uri: Optional[str] = None,
        content: Any = None,
        content_hash: Optional[str] = None,
        amount: float = 0.0,
        provider: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> str:
        """Record one tool call as evidence and, if it cost anything, as a cost line."""
        self._assert_open()
        digest = self.evidence(name, uri=uri or f"tool://{name}", type="tool_call", content=content, content_hash=content_hash, excerpt=excerpt)
        if amount:
            self.cost(amount, kind="tool_call", provider=provider or name)
        return digest

    def cost(
        self,
        amount: float,
        *,
        kind: str = "other",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> None:
        self._assert_open()
        if kind not in ("model_call", "tool_call", "other"):
            raise ValueError(f"cost kind must be model_call, tool_call or other, got {kind!r}")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            raise ValueError("cost amount must be a non-negative number")
        for label, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{label} must be a non-negative integer")
        item: Dict[str, Any] = {"kind": kind, "amount": float(amount)}
        if provider:
            item["provider"] = provider
        if model:
            item["model"] = model
        if tokens_in is not None:
            item["tokens_in"] = tokens_in
        if tokens_out is not None:
            item["tokens_out"] = tokens_out
        self._cost_items.append(item)

    # -- action and human review ----------------------------------------------

    def act(
        self,
        action: str,
        *,
        summary: Optional[str] = None,
        cost_centre: Optional[str] = None,
        alternatives: Optional[Sequence[str]] = None,
    ) -> None:
        """Record that the action was taken. Call it once, after the action succeeds."""
        self._assert_open()
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        if self._action is not None:
            raise RuntimeError(f"act() already called on decision {self.record_id} with {self._action!r}")
        self._action = action
        self._summary = summary
        self._cost_centre = cost_centre
        if alternatives:
            self._alternatives = list(alternatives)
        self._acted_at = _now()

    def require_human(self, *, reviewer: Optional[str] = None, note: Optional[str] = None) -> None:
        """Mark that a human must review this decision; the verdict arrives later via ``Warrant.human_verdict``."""
        self._assert_open()
        self._human = {"required": True}
        if reviewer:
            self._human["reviewer"] = reviewer
        if note:
            self._human["note"] = note

    @property
    def verdict(self) -> Optional[Verdict]:
        return self._verdict

    @property
    def acted(self) -> bool:
        return self._action is not None

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "Decision":
        self._token = _current.set(self)
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        self._closed = True
        record = self._build(exc)
        try:
            validate(record)
        except ValidationError as err:
            log.error("warrant decision %s produced an invalid record: %s", self.record_id, "; ".join(err.errors))
            if exc is None:
                raise
            return False
        self._client._submit(record)
        return False

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"decision {self.record_id} is closed")

    def _build(self, exc: Optional[BaseException]) -> Dict[str, Any]:
        client = self._client
        if exc is not None:
            status = "failed"
            action = self._action or "none"
            summary = self._summary or f"{type(exc).__name__} raised before the action completed"
        elif self._action is not None:
            status = "acted"
            action = self._action
            summary = self._summary
        else:
            status = "withheld"
            action = "none"
            summary = self._summary or ("withheld: mandate result was " + self._verdict.result if self._verdict else "withheld")

        decision: Dict[str, Any] = {"class": self.decision_class, "action": action, "subject": self.subject, "status": status}
        if summary:
            decision["summary"] = summary
        if self._alternatives:
            decision["alternatives"] = self._alternatives

        verdict = self._verdict or Verdict("unchecked")
        mandate: Dict[str, Any] = {"result": verdict.result}
        for key in ("policy_id", "policy_version", "clause", "reason"):
            value = getattr(verdict, key)
            if value:
                mandate[key] = value
        if verdict.flagged:
            mandate["flagged"] = True

        actor: Dict[str, Any] = {"name": client.agent.name, "version": client.agent.version}
        if client.agent.instance:
            actor["instance"] = client.agent.instance
        on_behalf_of = self._on_behalf_of or client.on_behalf_of
        if on_behalf_of:
            actor["on_behalf_of"] = on_behalf_of

        cost: Dict[str, Any] = {"amount": round(sum(i["amount"] for i in self._cost_items), 6), "currency": client.currency}
        if self._cost_items:
            cost["breakdown"] = self._cost_items
        if self._cost_centre:
            cost["cost_centre"] = self._cost_centre

        record: Dict[str, Any] = {
            "record_id": self.record_id,
            "record_type": "decision",
            "tenant": client.tenant,
            "stream": client.stream,
            "timestamp": self._acted_at or _now(),
            "schema_version": SCHEMA_VERSION,
            "origin": "live",
            "actor": actor,
            "decision": decision,
            "mandate": mandate,
            "evidence": self._evidence,
            "human": self._human,
            "cost": cost,
            "outcome": {"status": "pending"},
        }
        return client.redactor.apply(record) if client.redactor else record


def _hash_content(content: Any) -> str:
    try:
        return content_hash(content)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


class Warrant:
    """Entry point. One instance per stream; share it across decisions and threads.

    ``store`` is a path to a local SQLite file (default ``$WARRANT_STORE`` or
    ``.warrant/records.db``), or any object with ``write(records)`` for custom sinks.
    ``policy_bundle`` is a directory of CEL policy files (see ``warrant.policy``);
    ``policy`` is any object with ``evaluate(decision_class, inputs) -> Verdict``.
    Recording never blocks the caller: records are queued and written by a background
    thread, and spilled to ``spill_dir`` if the store is unavailable.
    """

    def __init__(
        self,
        stream: str,
        *,
        tenant: Optional[str] = None,
        store: Union[str, Path, Sink, None] = None,
        agent: Optional[AgentInfo] = None,
        on_behalf_of: Optional[str] = None,
        policy: Optional[PolicyEngine] = None,
        policy_bundle: Union[str, Path, None] = None,
        redact: Optional[Redactor] = None,
        currency: Optional[str] = None,
        spill_dir: Union[str, Path, None] = None,
        max_queue: int = 10_000,
        batch_size: int = 200,
        flush_interval: float = 0.2,
    ) -> None:
        if not isinstance(stream, str) or not stream:
            raise ValueError("stream must be a non-empty string")
        self.stream = stream
        self.tenant = tenant or os.environ.get("WARRANT_TENANT") or "local"
        self.currency = currency or os.environ.get("WARRANT_CURRENCY") or "USD"
        if not _CURRENCY_RE.match(self.currency):
            raise ValueError(f"currency must be a three-letter ISO code, got {self.currency!r}")
        self.agent = agent or _agent_from_env()
        self.on_behalf_of = on_behalf_of
        if policy is not None and policy_bundle is not None:
            raise ValueError("pass either policy or policy_bundle, not both")
        if policy_bundle is not None:
            from warrant.policy import CelPolicyEngine, PolicyBundle

            policy = CelPolicyEngine(PolicyBundle.load(policy_bundle))
        self.policy = policy
        self.redactor = redact

        if store is None or isinstance(store, (str, Path)):
            path = Path(store or os.environ.get("WARRANT_STORE") or ".warrant/records.db")
            self._store: Optional[SQLiteStore] = SQLiteStore(path)
            sink: Sink = self._store
            default_spill = path.parent / "spill"
        else:
            self._store = store if isinstance(store, SQLiteStore) else None
            sink = store
            default_spill = Path(".warrant/spill")
        self._emitter = Emitter(
            sink,
            Path(spill_dir) if spill_dir else default_spill,
            max_queue=max_queue,
            batch_size=batch_size,
            flush_interval=flush_interval,
        )
        self._emitter.start()

    # -- recording -----------------------------------------------------------

    def decide(
        self,
        decision_class: str,
        *,
        subject: str,
        on_behalf_of: Optional[str] = None,
        alternatives: Optional[Sequence[str]] = None,
    ) -> Decision:
        """Open a decision scope. Use as ``with w.decide("credit.approve", subject=...) as d:``."""
        return Decision(self, decision_class, subject, on_behalf_of=on_behalf_of, alternatives=alternatives)

    def outcome(
        self,
        *,
        label: str,
        subject: Optional[str] = None,
        decision_record_id: Optional[str] = None,
        observed_at: Union[str, datetime, None] = None,
        score: Optional[float] = None,
        source: Optional[str] = None,
    ) -> str:
        """Append an outcome record linked to a past decision. Returns the outcome record id."""
        if not isinstance(label, str) or not label:
            raise ValueError("label must be a non-empty string")
        target = self._resolve(subject, decision_record_id)
        outcome: Dict[str, Any] = {"status": "observed", "label": label, "observed_at": _as_timestamp(observed_at)}
        if score is not None:
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise ValueError("score must be a number")
            outcome["score"] = float(score)
        if source:
            outcome["source"] = source
        return self._append_linked("outcome", target, {"outcome": outcome})

    def human_verdict(
        self,
        *,
        reviewer: str,
        verdict: str,
        subject: Optional[str] = None,
        decision_record_id: Optional[str] = None,
        note: Optional[str] = None,
        at: Union[str, datetime, None] = None,
    ) -> str:
        """Append a human review verdict linked to a past decision. Returns the new record id."""
        if verdict not in HUMAN_VERDICTS:
            raise ValueError(f"verdict must be one of {HUMAN_VERDICTS}, got {verdict!r}")
        if not isinstance(reviewer, str) or not reviewer:
            raise ValueError("reviewer must be a non-empty string")
        target = self._resolve(subject, decision_record_id)
        human: Dict[str, Any] = {"required": True, "reviewer": reviewer, "verdict": verdict, "at": _as_timestamp(at)}
        if note:
            human["note"] = note
        return self._append_linked("human_verdict", target, {"human": human})

    def _resolve(self, subject: Optional[str], decision_record_id: Optional[str]) -> str:
        if decision_record_id:
            return decision_record_id
        if not subject:
            raise ValueError("pass subject or decision_record_id")
        if self._store is None:
            raise LookupError("subject lookup needs a local store; pass decision_record_id instead")
        self._emitter.flush()
        found = self._store.find_decision(self.stream, subject)
        if found is None:
            raise LookupError(f"no decision for subject {subject!r} in stream {self.stream!r}")
        return found

    def _append_linked(self, record_type: str, decision_record_id: str, section: Dict[str, Any]) -> str:
        record: Dict[str, Any] = {
            "record_id": ulid(),
            "record_type": record_type,
            "tenant": self.tenant,
            "stream": self.stream,
            "timestamp": _now(),
            "schema_version": SCHEMA_VERSION,
            "origin": "live",
            "references": {"decision_record_id": decision_record_id},
        }
        record.update(section)
        if self.redactor:
            record = self.redactor.apply(record)
        validate(record)
        self._submit(record)
        return record["record_id"]

    def _submit(self, record: Dict[str, Any]) -> None:
        self._emitter.submit(record)

    # -- lifecycle -----------------------------------------------------------

    @property
    def store(self) -> Optional[SQLiteStore]:
        return self._store

    def flush(self, timeout: Optional[float] = 5.0) -> bool:
        """Wait until queued records are written or safely spilled. Returns False on timeout."""
        return self._emitter.flush(timeout)

    def stats(self) -> Dict[str, int]:
        return self._emitter.stats()

    def close(self, timeout: Optional[float] = 5.0) -> None:
        self._emitter.close(timeout)
        if self._store is not None:
            self._store.close()

    def __enter__(self) -> "Warrant":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


_warned_default_agent = False


def _agent_from_env() -> AgentInfo:
    global _warned_default_agent
    name = os.environ.get("WARRANT_AGENT_NAME")
    version = os.environ.get("WARRANT_AGENT_VERSION")
    instance = os.environ.get("WARRANT_AGENT_INSTANCE")
    if not name or not version:
        name = name or (Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "unnamed-agent")
        version = version or "0"
        if not _warned_default_agent:
            log.warning("warrant: agent identity not set; recording as %s@%s. Pass agent=AgentInfo(...) or set WARRANT_AGENT_NAME/VERSION", name, version)
            _warned_default_agent = True
    return AgentInfo(name=name, version=version, instance=instance)
