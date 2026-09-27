"""The Warrant client and the ``decide()`` context manager."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Type, Union

from warrant.emit import Emitter, Sink
from warrant.hashing import SALT_BYTES, content_hash, record_hash, salted_hash
from warrant.ids import ULID_RE, ulid
from warrant.redaction import Redactor
from warrant.schema import SCHEMA_VERSION, ValidationError, validate
from warrant.store import SQLiteStore, refuse_mangled_url

ROUTES = ("auto", "human", "model", "deferred")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

log = logging.getLogger("warrant")

_CLASS_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_TYPES = ("model_call", "tool_call", "document", "web", "other", "record", "mandate", "attestation", "human_review")
MANDATE_RESULTS = ("allow", "deny", "escalate", "unchecked")
HUMAN_VERDICTS = ("approve", "reject", "amend")

_current: "contextvars.ContextVar[Optional[Decision]]" = contextvars.ContextVar("warrant_decision", default=None)


class Unreplayable(Exception):
    """Frozen replay could not serve a tool call from recorded evidence."""


class NotWarranted(RuntimeError):
    """A decision tried to act without a warrant: fail closed (ADR level 2).

    ``state`` is the lifecycle state it reached instead, and ``unmet`` the verifiable obligations
    no admitted evidence satisfied.
    """

    def __init__(self, record_id: str, state: Optional[str], unmet: Sequence[str]) -> None:
        self.record_id, self.state, self.unmet = record_id, state, list(unmet)
        detail = f"; unmet: {', '.join(unmet)}" if unmet else ""
        super().__init__(f"decision {record_id} is {state or 'not assessed'}, not warranted{detail}")


class CitationError(ValueError):
    """An upstream record could not be cited: unsealed, altered, or its signature does not verify."""


@dataclass(frozen=True)
class WarrantState:
    """What :meth:`Decision.warrant` found: the state reached and why."""

    state: Optional[str]
    met: Tuple[str, ...]
    unmet: Tuple[str, ...]
    rejected: Tuple[Tuple[str, str], ...]

    @property
    def warranted(self) -> bool:
        return self.state in ("warranted", "committed")


class ReplaySource(Protocol):
    """Serves recorded tool results during frozen replay."""

    def lookup(self, name: str) -> Any:
        """Return the next recorded result for tool ``name`` or raise ``Unreplayable``."""


def encode_blob(content: Any) -> Dict[str, Any]:
    """JSON-safe envelope for captured evidence content."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        import base64

        return {"encoding": "base64", "data": base64.b64encode(bytes(content)).decode("ascii")}
    if isinstance(content, str):
        return {"encoding": "utf8", "data": content}
    return {"encoding": "json", "data": content}


def decode_blob(blob: Dict[str, Any]) -> Any:
    encoding = blob.get("encoding")
    if encoding == "base64":
        import base64

        return base64.b64decode(blob["data"])
    if encoding in ("utf8", "json"):
        return blob["data"]
    raise ValueError(f"unknown blob encoding {encoding!r}")


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
    obligations: Tuple[Dict[str, Any], ...] = ()
    enforce: bool = False
    retention: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.result not in MANDATE_RESULTS:
            raise ValueError(f"mandate result must be one of {MANDATE_RESULTS}, got {self.result!r}")

    @property
    def allowed(self) -> bool:
        """True for ``allow``. ``unchecked`` is not allowed: a policy engine has to say so."""
        return self.result == "allow"


class PolicyEngine(Protocol):
    def evaluate(
        self, decision_class: str, inputs: Mapping[str, Any], at: Optional[str] = None
    ) -> Verdict:
        """Evaluate the mandate for one decision class against the supplied inputs."""


@dataclass(frozen=True)
class AgentInfo:
    """Who is acting. ``identity`` is ``(registry, id)`` or ``(registry, id, uri)`` in a registry the
    relying party recognises."""

    name: str
    version: str
    instance: Optional[str] = None
    model: Optional[str] = None
    runtime: Optional[str] = None
    identity: Optional[Tuple[str, ...]] = None


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
        replay_source: Optional[ReplaySource] = None,
        record_id: Optional[str] = None,
    ) -> None:
        if not _CLASS_RE.match(decision_class):
            raise ValueError(f"decision class must look like 'credit.approve', got {decision_class!r}")
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        if record_id is not None and not (isinstance(record_id, str) and ULID_RE.match(record_id)):
            raise ValueError(f"record_id must be a 26-character ULID, got {record_id!r}")
        self._client = client
        self.record_id = record_id or ulid()
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
        self._inputs: Optional[Dict[str, Any]] = None
        self._answers: List[Dict[str, Any]] = []
        self._question_set: Optional[Dict[str, str]] = None
        self._state_digest: Optional[str] = None
        self._state_ref: Optional[str] = None
        self._route: Optional[str] = None
        self._blobs: Dict[str, Dict[str, Any]] = {}
        self._claims: List[Dict[str, Any]] = []
        self._parents: List[Dict[str, Any]] = []
        self._obligations: List[Dict[str, Any]] = []
        self._retention: Optional[Dict[str, Any]] = None
        self._warrant_at: Optional[str] = None
        self._replay_source = replay_source
        self._token: Optional[contextvars.Token] = None
        self._closed = False

    # -- mandate -------------------------------------------------------------

    def check(self, **inputs: Any) -> Verdict:
        """Ask the policy engine whether this action is within mandate. Synchronous; runs locally."""
        self._assert_open()
        if self._client.capture_inputs and self._inputs is None:
            self.set_inputs(inputs)
        engine = self._client.policy
        if engine is None:
            self._verdict = Verdict("unchecked", reason="no policy engine configured")
        else:
            # The decision's own moment, not the reader's clock: an effective-dated policy must
            # judge this decision by the rules in force when it was made.
            try:
                self._verdict = engine.evaluate(self.decision_class, inputs, at=self._opened_at)
            except TypeError:
                self._verdict = engine.evaluate(self.decision_class, inputs)  # older engine
        known = {o["id"] for o in self._obligations}
        for ob in self._verdict.obligations:
            if ob["id"] not in known:
                self._obligations.append(dict(ob))
        if self._verdict.retention and self._retention is None:
            self._apply_policy_retention(self._verdict.retention)
        return self._verdict

    def set_inputs(self, inputs: Mapping[str, Any]) -> None:
        """Record the inputs this decision is made on, so it can be replayed. Must be JSON-serialisable."""
        self._assert_open()
        try:
            self._inputs = json.loads(json.dumps(dict(inputs)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"inputs must be JSON-serialisable: {exc}") from exc

    @property
    def inputs(self) -> Optional[Dict[str, Any]]:
        return self._inputs

    @property
    def replaying(self) -> bool:
        return self._replay_source is not None

    # -- evidence and cost ---------------------------------------------------

    def tool(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        uri: Optional[str] = None,
        amount: float = 0.0,
        provider: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> Any:
        """Run a tool through Warrant. Live: calls ``fn``, records the result as evidence and, with
        ``capture_evidence`` on, keeps the content locally for replay. Frozen replay: returns the
        recorded result without calling ``fn``, or raises ``Unreplayable``."""
        self._assert_open()
        if self._replay_source is not None:
            result = self._replay_source.lookup(name)
        else:
            result = fn()
        digest = self.evidence(name, uri=uri or f"tool://{name}", type="tool_call", content=result, excerpt=excerpt)
        if self._client.capture_evidence and self._replay_source is None:
            self._blobs[digest] = encode_blob(result)
        if amount:
            self.cost(amount, kind="tool_call", provider=provider or name)
        return result

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
        provider: Optional[str] = None,
        obligation: Optional[str] = None,
        sensitive: bool = False,
    ) -> str:
        """Attach evidence by reference. Content is hashed here and never stored. Returns the hash.

        ``provider`` names who produced it; the acting agent's own evidence is never admitted for its
        own obligation. ``obligation`` is the obligation id it is offered against. ``sensitive``
        uses a salted digest (ADR 4): the salt goes to the store's sidecar, never onto the record,
        so erasing the sidecar entry unlinks the digest from the data while the record still verifies.
        """
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
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise ValueError("provider must be a non-empty string")
        if obligation is not None and (not isinstance(obligation, str) or not obligation):
            raise ValueError("obligation must be a non-empty string")
        if sensitive:
            if content is None:
                raise ValueError("sensitive evidence needs its content: a salted digest cannot be made from a bare hash")
            if excerpt is not None:
                raise ValueError("sensitive evidence cannot carry an excerpt; the excerpt would be the personal data in clear")
            salt = os.urandom(SALT_BYTES)
            try:
                digest = salted_hash(content, salt)
            except TypeError as exc:
                raise ValueError(str(exc)) from exc
            sidecar = encode_blob(content) if self._client.capture_evidence else {}
            sidecar["salt"] = salt.hex()
            self._blobs[digest] = sidecar
        else:
            digest = content_hash if content_hash is not None else _hash_content(content)
        item: Dict[str, Any] = {"name": name, "type": type, "uri": uri, "content_hash": digest}
        if sensitive:
            item["salted"] = True
        if provider is not None:
            item["provider"] = provider
        if obligation is not None:
            item["obligation"] = obligation
        if retrieved_at is not None:
            item["retrieved_at"] = _as_timestamp(retrieved_at)
        if excerpt is not None:
            if not isinstance(excerpt, str):
                raise TypeError("excerpt must be a string")
            item["excerpt"] = excerpt
        self._evidence.append(item)
        return digest

    def salt_for(self, digest: str) -> Optional[str]:
        """The hex salt behind a sensitive item's digest, for a caller that keeps its own sidecar."""
        return (self._blobs.get(digest) or {}).get("salt")

    # -- ADR: claims, handoffs, obligations, the warrant ----------------------

    def claim(self, claim: str, value: Any = None) -> None:
        """Record what the agent asserts. A claim is never evidence, here or downstream (ADR 1.2)."""
        self._assert_open()
        if not isinstance(claim, str) or not claim:
            raise ValueError("claim must be a non-empty string")
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"claim value must be JSON-serialisable: {exc}") from exc
        item: Dict[str, Any] = {"claim": claim}
        if value is not None:
            item["value"] = value
        self._claims.append(item)

    def cite(
        self,
        parent: Mapping[str, Any],
        *,
        keyring: Any = None,
        name: Optional[str] = None,
        obligation: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        """Rely on an upstream agent's sealed record, possibly another organisation's (ADR rule 5).

        The parent is cited by id and sealed hash; its claims are not copied and never become
        evidence. With ``keyring`` the parent's issuer signature must verify, and the issuer's name
        comes from the key set. ``state`` overrides the parent's own ``verdict.state`` when a later
        transition moved it. Returns the cited hash.
        """
        self._assert_open()
        seal = parent.get("seal") if isinstance(parent, Mapping) else None
        if not isinstance(seal, Mapping) or not seal.get("hash"):
            raise CitationError("the parent is not sealed; cite a record exported from its issuer's store")
        if record_hash(parent, seal.get("prev_hash")) != seal["hash"]:
            raise CitationError(f"parent {parent.get('record_id')} does not match its own seal; it was altered")
        issuer = parent.get("tenant")
        key_id = seal.get("key_id")
        if keyring is not None:
            from warrant.signing import verify_seal

            ok, detail = verify_seal(parent, keyring)
            if not ok:
                raise CitationError(f"parent {parent.get('record_id')}: {detail}")
            issuer = detail
        cited_state = state or (parent.get("verdict") or {}).get("state")
        entry: Dict[str, Any] = {"record_id": parent["record_id"], "hash": seal["hash"]}
        if issuer:
            entry["issuer"] = issuer
        if key_id:
            entry["key_id"] = key_id
        if cited_state:
            entry["state"] = cited_state
        self._parents.append(entry)
        self.evidence(
            name or f"{issuer or 'upstream'}:{(parent.get('decision') or {}).get('class', 'record')}",
            uri=f"adr://{issuer or 'unknown'}/{parent['record_id']}#{seal['hash']}",
            type="record",
            content_hash=seal["hash"],
            # Only a verified signature names a provider. An unauthenticated citation is recorded,
            # but without a provider it counts as the agent's own say-so and cannot authorise.
            provider=issuer if keyring is not None else None,
            obligation=obligation,
            retrieved_at=parent.get("timestamp"),
        )
        self._evidence[-1]["parent"] = {k: v for k, v in (("record_id", parent["record_id"]), ("hash", seal["hash"]), ("issuer", issuer)) if v}
        return seal["hash"]

    def obligation(
        self,
        obligation_id: str,
        *,
        requires: str,
        kind: str = "verifiable",
        providers: Sequence[str] = (),
        max_age_seconds: Optional[int] = None,
        name: Optional[str] = None,
        clause: Optional[str] = None,
    ) -> None:
        """Declare an obligation by hand. Policy bundles normally supply these through ``check()``."""
        self._assert_open()
        if not isinstance(obligation_id, str) or not obligation_id:
            raise ValueError("obligation id must be a non-empty string")
        if any(o["id"] == obligation_id for o in self._obligations):
            raise ValueError(f"obligation {obligation_id!r} is already declared")
        if requires not in EVIDENCE_TYPES:
            raise ValueError(f"requires must be one of {EVIDENCE_TYPES}, got {requires!r}")
        if kind not in ("verifiable", "advisory"):
            raise ValueError("kind must be verifiable or advisory")
        if "self" in providers:
            raise ValueError("'self' cannot be a qualified provider")
        if max_age_seconds is not None and (not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds < 0):
            raise ValueError("max_age_seconds must be a non-negative integer")
        ob: Dict[str, Any] = {"id": obligation_id, "requires": requires, "kind": kind}
        if providers:
            ob["providers"] = list(providers)
        for key, value in (("max_age_seconds", max_age_seconds), ("name", name), ("clause", clause)):
            if value is not None:
                ob[key] = value
        self._obligations.append(ob)

    def warrant(self) -> WarrantState:
        """Apply the admissibility rules to the evidence so far and report the state reached."""
        self._assert_open()
        from warrant.admissibility import assess

        self._warrant_at = _now()
        self._auto_offer()
        draft = self._draft(status="withheld")
        assessment = assess(draft, at=self._warrant_at)
        rejected = tuple(
            (draft["evidence"][i].get("name", str(i)), a.get("reason", ""))
            for i, a in sorted(assessment.admissions.items()) if a["status"] == "rejected"
        )
        return WarrantState(assessment.state, tuple(assessment.met), tuple(assessment.unmet), rejected)

    def commit(
        self,
        action: str,
        *,
        summary: Optional[str] = None,
        cost_centre: Optional[str] = None,
        alternatives: Optional[Sequence[str]] = None,
        route: Optional[str] = None,
    ) -> None:
        """Act only on a warrant. Raises :class:`NotWarranted` instead of recording the action."""
        state = self.warrant()
        if not state.warranted:
            raise NotWarranted(self.record_id, state.state, state.unmet)
        self._act(action, summary=summary, cost_centre=cost_centre, alternatives=alternatives, route=route)

    def retention(self, retention_class: str, *, retain_until: Union[str, datetime, None] = None, legal_hold: bool = False) -> None:
        """Record the retention duty this decision falls under (ADR 1). The store never deletes."""
        self._assert_open()
        if not isinstance(retention_class, str) or not retention_class:
            raise ValueError("retention class must be a non-empty string")
        item: Dict[str, Any] = {"class": retention_class}
        if retain_until is not None:
            item["retain_until"] = _as_timestamp(retain_until)
        if legal_hold:
            item["legal_hold"] = True
        self._retention = item

    def _apply_policy_retention(self, policy_retention: Mapping[str, Any]) -> None:
        item: Dict[str, Any] = {"class": policy_retention["class"]}
        seconds = policy_retention.get("seconds")
        if seconds:
            from datetime import timedelta

            opened = datetime.fromisoformat(self._opened_at.replace("Z", "+00:00"))
            item["retain_until"] = _as_timestamp(opened + timedelta(seconds=seconds))
        self._retention = item

    def _auto_offer(self) -> None:
        """Offer an unassigned item to the one obligation it can only be meant for.

        Matching is by the obligation's ``name`` first, then by type when exactly one obligation
        requires that type. The assignment is written onto the record, so the verifier judges the
        same pairing the issuer did.
        """
        for item in self._evidence:
            if "obligation" in item or item.get("type") == "model_call":
                continue
            by_name = [o for o in self._obligations if o.get("name") and o["name"] == item["name"]]
            by_type = [o for o in self._obligations if not o.get("name") and o["requires"] == item["type"]]
            match = by_name if by_name else by_type
            if len(match) == 1:
                item["obligation"] = match[0]["id"]

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

    def question_set(self, set_id: str, version: str) -> None:
        """Name the registered, versioned set of questions this decision was made by answering.

        Stamped on the record so a reliability curve can be scoped to one version, and a silent
        edit to a question cannot invalidate a historical comparison.
        """
        self._assert_open()
        if not isinstance(set_id, str) or not set_id:
            raise ValueError("question set id must be a non-empty string")
        if not isinstance(version, str) or not version:
            raise ValueError("question set version must be a non-empty string")
        self._question_set = {"id": set_id, "version": version}

    def answer(
        self,
        question: str,
        value: Union[str, int, float, bool],
        *,
        confidence: Optional[float] = None,
        distribution: Optional[Mapping[Union[str, int, float, bool], float]] = None,
    ) -> None:
        """Record one typed answer, with the full distribution rather than only the winning value.

        Reliability analysis is impossible without the runners-up, and the spread across a
        distribution is a better escalation trigger than top-1 confidence alone. ``confidence``
        is what :mod:`warrant.calibrate` measures against the realised outcome.
        """
        self._assert_open()
        if not isinstance(question, str) or not question:
            raise ValueError("question must be a non-empty string")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError("answer value must be a string, number or boolean")
        item: Dict[str, Any] = {"question": question, "value": value}
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise TypeError("confidence must be a number")
            if not 0.0 <= float(confidence) <= 1.0:
                raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
            item["confidence"] = float(confidence)
        if distribution is not None:
            entries = []
            for outcome_value, probability in distribution.items():
                if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                    raise TypeError("distribution probabilities must be numbers")
                if not 0.0 <= float(probability) <= 1.0:
                    raise ValueError(f"distribution probability out of range: {probability}")
                entries.append({"value": outcome_value, "p": float(probability)})
            if entries:
                item["distribution"] = entries
        self._answers.append(item)

    def state(self, *, digest: str, ref: Optional[str] = None) -> None:
        """Record the hash of the exact state this decision was made on, and where the snapshot lives.

        The chain proves the state was not altered; the snapshot allows replay. They are separate
        on purpose: a snapshot carries its own retention and residency and can be redacted or
        expired without breaking the chain.
        """
        self._assert_open()
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ValueError("state digest must be a sha256 hex string")
        if ref is not None and (not isinstance(ref, str) or not ref):
            raise ValueError("state ref must be a non-empty string")
        self._state_digest = digest
        self._state_ref = ref

    def act(
        self,
        action: str,
        *,
        summary: Optional[str] = None,
        cost_centre: Optional[str] = None,
        alternatives: Optional[Sequence[str]] = None,
        route: Optional[str] = None,
    ) -> None:
        """Record that the action was taken. Call it once, after the action succeeds.

        ``route`` says where the policy sent this decision — ``auto``, ``human``, ``model`` or
        ``deferred`` — which is a different question from ``action``, what was decided.

        Where the policy for this class sets ``enforce: true``, or the client was created with
        ``enforce=True``, acting without a warrant raises :class:`NotWarranted` (fail closed).
        """
        if self._enforced():
            state = self.warrant()
            if not state.warranted:
                raise NotWarranted(self.record_id, state.state, state.unmet)
        self._act(action, summary=summary, cost_centre=cost_centre, alternatives=alternatives, route=route)

    def _enforced(self) -> bool:
        return bool(self._client.enforce or (self._verdict is not None and self._verdict.enforce))

    def _act(
        self,
        action: str,
        *,
        summary: Optional[str] = None,
        cost_centre: Optional[str] = None,
        alternatives: Optional[Sequence[str]] = None,
        route: Optional[str] = None,
    ) -> None:
        self._assert_open()
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        if self._action is not None:
            raise RuntimeError(f"act() already called on decision {self.record_id} with {self._action!r}")
        if route is not None and route not in ROUTES:
            raise ValueError(f"route must be one of {', '.join(ROUTES)}, got {route!r}")
        self._action = action
        self._summary = summary
        self._cost_centre = cost_centre
        self._route = route
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

    def human_review(
        self,
        reviewer: str,
        *,
        shown: Sequence[str],
        verdict: str = "approve",
        note: Optional[str] = None,
        at: Union[str, datetime, None] = None,
    ) -> None:
        """Record that a named person decided this, having been shown exactly ``shown`` (ADR rule 7).

        For decisions where the person's act *is* the decision (a reviewer approving a determination),
        rather than one that arrives later as a ``human_verdict`` or a transition. ``shown`` is the
        list of digests of the material they were shown; each must be on this record for a
        ``human_review`` obligation to be met.
        """
        self._assert_open()
        if not isinstance(reviewer, str) or not reviewer:
            raise ValueError("reviewer must be a non-empty string")
        if verdict not in HUMAN_VERDICTS:
            raise ValueError(f"verdict must be one of {HUMAN_VERDICTS}, got {verdict!r}")
        shown_list = list(shown)
        if any(not isinstance(d, str) or not SHA256_RE.match(d) for d in shown_list):
            raise ValueError("shown must be sha256 digests of the material the reviewer saw")
        self._human = {"required": True, "reviewer": reviewer, "verdict": verdict, "at": _as_timestamp(at), "shown": shown_list}
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
            validate({k: v for k, v in record.items() if k != "_blobs"})
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

    def _attach_verdict(self, record: Dict[str, Any], verdict: Verdict) -> None:
        """Obligations with met/unmet, admissions on the evidence, and the verdict (ADR 1.4, 2, 3)."""
        from warrant.admissibility import assess

        record["obligations"] = [dict(o) for o in self._obligations]
        at = self._warrant_at or _now()
        record["verdict"] = {"state": "proposed", "at": at}
        assessment = assess(record, at=at)
        record["obligations"] = assessment.obligations
        for index, admission in assessment.admissions.items():
            record["evidence"][index]["admission"] = admission
        verdict_out: Dict[str, Any] = {"state": assessment.state or "proposed", "decided_by": _decided_by(verdict), "at": at}
        if assessment.met:
            verdict_out["met"] = assessment.met
        if assessment.unmet:
            verdict_out["unmet"] = assessment.unmet
        verdict_out["history"] = [{"state": s, "at": at} for s in assessment.history]
        record["verdict"] = verdict_out

    def _build(self, exc: Optional[BaseException]) -> Dict[str, Any]:
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
        if self._obligations:
            self._auto_offer()
        return self._compose(status, action, summary, final=True)

    def _draft(self, *, status: str) -> Dict[str, Any]:
        """The record as it would be if the scope closed now without acting. For ``warrant()``."""
        return self._compose(status, self._action or "none", self._summary, final=False)

    def _compose(self, status: str, action: str, summary: Optional[str], *, final: bool) -> Dict[str, Any]:
        client = self._client
        decision: Dict[str, Any] = {"class": self.decision_class, "action": action, "subject": self.subject, "status": status}
        if summary:
            decision["summary"] = summary
        if self._alternatives:
            decision["alternatives"] = self._alternatives
        if self._inputs is not None:
            decision["inputs"] = self._inputs
        if self._route is not None:
            decision["route"] = self._route
        if self._question_set is not None:
            decision["question_set"] = self._question_set
        if self._state_digest is not None:
            decision["state_digest"] = self._state_digest
        if self._state_ref is not None:
            decision["state_ref"] = self._state_ref
        if self._answers:
            decision["answers"] = list(self._answers)
        if self._claims:
            decision["claims"] = list(self._claims)

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
        if client.agent.model:
            actor["model"] = client.agent.model
        if client.agent.runtime:
            actor["runtime"] = client.agent.runtime
        if client.agent.identity:
            registry, identity_id, *rest = client.agent.identity
            actor["identity"] = {"registry": registry, "id": identity_id}
            if rest and rest[0]:
                actor["identity"]["uri"] = rest[0]
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
            "evidence": [dict(e) for e in self._evidence],
            "human": dict(self._human),
            "cost": cost,
            "outcome": {"status": "pending"},
        }
        if self._parents:
            record["parents"] = [dict(p) for p in self._parents]
        if self._retention:
            record["retention"] = dict(self._retention)
        if self._obligations or self._warrant_at:
            self._attach_verdict(record, verdict)
        if not final:
            return record
        if client.redactor:
            record = client.redactor.apply(record)
        if self._blobs:
            record["_blobs"] = self._blobs
        return record


def _decided_by(verdict: Verdict) -> str:
    if verdict.policy_id:
        return f"policy:{verdict.policy_id}@{verdict.policy_version}"
    return "rules:adr/0.2"


def _hash_content(content: Any) -> str:
    try:
        return content_hash(content)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


class Warrant:
    """Entry point. One instance per stream; share it across decisions and threads.

    ``store`` is a path to a local SQLite file (default ``$WARRANT_STORE`` or
    ``.warrant/records.db``), a ``postgresql://`` DSN, a collector URL
    (``https://...``, authenticated with ``token`` or ``$WARRANT_TOKEN``), or any
    object with ``write(records)`` for custom sinks.
    ``policy_bundle`` is a directory of CEL policy files (see ``warrant.policy``);
    ``policy`` is any object with ``evaluate(decision_class, inputs, at=None) -> Verdict``.
    ``capture_inputs`` stores ``check()`` inputs on the record and ``capture_evidence``
    keeps tool results from ``d.tool()`` in the local store, both for replay; turn them
    on in development and staging, not where payloads must stay out of the store.
    Recording never blocks the caller: records are queued and written by a background
    thread, and spilled to ``spill_dir`` if the store is unavailable.
    """

    def __init__(
        self,
        stream: str,
        *,
        tenant: Optional[str] = None,
        store: Union[str, Path, Sink, None] = None,
        token: Optional[str] = None,
        agent: Optional[AgentInfo] = None,
        on_behalf_of: Optional[str] = None,
        policy: Optional[PolicyEngine] = None,
        policy_bundle: Union[str, Path, None] = None,
        redact: Optional[Redactor] = None,
        currency: Optional[str] = None,
        capture_inputs: bool = False,
        capture_evidence: bool = False,
        spill_dir: Union[str, Path, None] = None,
        signing_key: Any = None,
        enforce: bool = False,
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
        self.capture_inputs = capture_inputs
        self.capture_evidence = capture_evidence
        self.enforce = enforce
        from warrant.signing import resolve_signing_key

        signer = resolve_signing_key(signing_key)

        if store is None or isinstance(store, (str, Path)):
            target = str(store or os.environ.get("WARRANT_STORE") or ".warrant/records.db")
            refuse_mangled_url(target)
            if target.startswith(("http://", "https://")):
                from warrant.sinks import HttpSink

                self._store = None
                sink = HttpSink(target, token)
                default_spill = Path(".warrant/spill")
            elif target.startswith(("postgres://", "postgresql://")):
                from warrant.store import open_store

                self._store = open_store(target, signer=signer)
                sink = self._store
                default_spill = Path(".warrant/spill")
            else:
                path = Path(target)
                self._store = SQLiteStore(path, signer=signer)
                sink = self._store
                default_spill = path.parent / "spill"
        else:
            self._store = store if isinstance(store, SQLiteStore) else None
            sink = store
            default_spill = Path(".warrant/spill")
        if signer is not None and self._store is None:
            raise ValueError(
                "signing_key signs where records are sealed: pass it to the collector or store that seals them "
                "(warrant collector --signing-key), not to a client that sends records elsewhere"
            )
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
        record_id: Optional[str] = None,
    ) -> Decision:
        """Open a decision scope. Use as ``with w.decide("credit.approve", subject=...) as d:``.

        ``record_id`` lets a caller that may record the same event twice (a retried delivery,
        a re-run step) supply a deterministic ULID, so the store's duplicate check absorbs the repeat.
        """
        return Decision(self, decision_class, subject, on_behalf_of=on_behalf_of, alternatives=alternatives, record_id=record_id)

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
        record_id: Optional[str] = None,
    ) -> str:
        """Append a human review verdict linked to a past decision. Returns the new record id.

        ``record_id`` is for callers that may deliver the same verdict twice; see ``decide``.
        """
        if verdict not in HUMAN_VERDICTS:
            raise ValueError(f"verdict must be one of {HUMAN_VERDICTS}, got {verdict!r}")
        if not isinstance(reviewer, str) or not reviewer:
            raise ValueError("reviewer must be a non-empty string")
        if record_id is not None and not (isinstance(record_id, str) and ULID_RE.match(record_id)):
            raise ValueError(f"record_id must be a 26-character ULID, got {record_id!r}")
        target = self._resolve(subject, decision_record_id)
        human: Dict[str, Any] = {"required": True, "reviewer": reviewer, "verdict": verdict, "at": _as_timestamp(at)}
        if note:
            human["note"] = note
        return self._append_linked("human_verdict", target, {"human": human}, record_id=record_id)

    def transition(
        self,
        decision_record_id: str,
        to_state: str,
        *,
        decided_by: str,
        from_state: Optional[str] = None,
        reviewer: Optional[str] = None,
        shown: Optional[Sequence[str]] = None,
        reason: Optional[str] = None,
        record_id: Optional[str] = None,
        decision: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Append a lifecycle transition for an earlier decision (ADR 3). History is never edited.

        A client recording to a collector has no store to read the decision from: pass ``from_state``
        and, to leave ``pending_evidence`` for ``warranted``, the sealed ``decision`` record itself, so
        the rules can confirm only a person was missing.

        ``from_state`` is read from the store when one is local; a client recording to a collector
        must pass it. Leaving ``escalated`` for ``warranted`` needs a named ``reviewer`` and the
        digests they were ``shown`` (rule 7). ``committed`` is reachable only from ``warranted``.
        """
        from warrant.admissibility import STATES, check_transition, legal

        if to_state not in STATES:
            raise ValueError(f"to_state must be one of {STATES}, got {to_state!r}")
        if not isinstance(decided_by, str) or not decided_by:
            raise ValueError("decided_by must name who decided, e.g. human:a.rao or policy:CR-07@2026.4")
        if record_id is not None and not (isinstance(record_id, str) and ULID_RE.match(record_id)):
            raise ValueError(f"record_id must be a 26-character ULID, got {record_id!r}")
        if decision is not None and decision.get("record_id") != decision_record_id:
            raise ValueError("decision is not the record named by decision_record_id")
        if self._store is not None:
            self._emitter.flush()
            decision = self._store.get(decision_record_id)
            if decision is None:
                raise LookupError(f"no decision {decision_record_id} in the store")
            current = current_state(self._store, decision)
            if from_state is not None and from_state != current:
                raise ValueError(f"decision {decision_record_id} is {current}, not {from_state}")
            from_state = current
        if from_state is None:
            raise ValueError("pass from_state: there is no local store to read the current state from")
        if not legal(from_state, to_state):
            raise ValueError(f"illegal transition {from_state} -> {to_state}")
        human: Optional[Dict[str, Any]] = None
        if reviewer is not None or shown is not None:
            if not isinstance(reviewer, str) or not reviewer:
                raise ValueError("a human transition needs the reviewer's name")
            shown_list = list(shown or [])
            if any(not isinstance(d, str) or not SHA256_RE.match(d) for d in shown_list):
                raise ValueError("shown must be sha256 digests of the material the reviewer saw")
            human = {"required": True, "reviewer": reviewer, "shown": shown_list, "at": _now(),
                     "verdict": "reject" if to_state == "refused" else "approve"}
        verdict: Dict[str, Any] = {"state": to_state, "from_state": from_state, "decided_by": decided_by, "at": _now()}
        if reason:
            verdict["reason"] = reason
        section: Dict[str, Any] = {"verdict": verdict}
        if human is not None:
            section["human"] = human
        if decision is not None:
            problem = check_transition(decision, from_state, section)
            if problem:
                raise ValueError(problem)
        elif to_state == "warranted" and from_state == "escalated" and human is None:
            raise ValueError("leaving escalated for warranted needs a named reviewer and the digests they were shown")
        elif to_state == "warranted" and from_state == "pending_evidence":
            raise ValueError("leaving pending_evidence for warranted needs the decision record (decision=), to confirm only a person was missing")
        return self._append_linked("transition", decision_record_id, section, record_id=record_id)

    def _resolve(self, subject: Optional[str], decision_record_id: Optional[str]) -> str:
        if decision_record_id:
            return decision_record_id
        if not subject:
            raise ValueError("pass subject or decision_record_id")
        if self._store is None:
            raise LookupError("subject lookup needs a store; when recording to a collector pass decision_record_id instead")
        self._emitter.flush()
        found = self._store.find_decision(self.stream, subject, self.tenant)
        if found is None:
            raise LookupError(f"no decision for subject {subject!r} in stream {self.stream!r}")
        return found

    def _append_linked(self, record_type: str, decision_record_id: str, section: Dict[str, Any], *, record_id: Optional[str] = None) -> str:
        record: Dict[str, Any] = {
            "record_id": record_id or ulid(),
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
    def store(self):
        """The local or PostgreSQL store, or None when recording to a collector."""
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


def current_state(store: Any, decision: Mapping[str, Any]) -> str:
    """A decision's lifecycle state now: its own verdict, advanced by any later transition records."""
    state = (decision.get("verdict") or {}).get("state") or "proposed"
    for linked in store.linked(decision["record_id"]):
        if linked.get("record_type") == "transition":
            state = (linked.get("verdict") or {}).get("state", state)
    return state


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
