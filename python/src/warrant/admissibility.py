"""ADR admissibility and lifecycle: which evidence counts, and what state a decision may reach.

Pure functions over a record, so the SDK that writes a verdict and the verifier that recomputes it
cannot disagree about the rules (ADR 2 and 3). Nothing here reads a clock, a store or a network:
freshness is judged at the verdict's own time, and a parent's signature is the verifier's job.

The seven rules, applied in order to each item offered against an obligation; the first that fails
is the rejection reason:

1. ``self_attested``        the acting agent supplied it (provider ``self``, the actor's own name, or none)
2. ``unqualified_provider`` the obligation names providers and this is not one of them
3. ``stale`` / ``no_timestamp`` older than the obligation allows at the verdict time
4. ``missing_digest``       no content hash
5. ``parent_not_cited`` / ``parent_not_warranted``  an upstream record not cited, or not warranted
6. ``wrong_type``           not the evidence type (or name) the obligation requires
7. ``unnamed_reviewer`` / ``material_not_linked``   a human obligation without a named reviewer
                            linked, by digest, to exactly what they were shown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

STATES = ("proposed", "pending_evidence", "escalated", "warranted", "refused", "committed")
TERMINAL = ("refused", "committed")
AUTHORISING = ("warranted", "committed")

#: Legal lifecycle edges. ``committed`` is reachable only from ``warranted``.
EDGES: Dict[str, Tuple[str, ...]] = {
    "proposed": ("pending_evidence", "escalated", "warranted", "refused"),
    "pending_evidence": ("warranted", "refused", "escalated"),
    "escalated": ("warranted", "refused"),
    "warranted": ("committed", "refused"),
    "refused": (),
    "committed": (),
}

REASONS = (
    "self_attested",
    "unqualified_provider",
    "stale",
    "no_timestamp",
    "missing_digest",
    "parent_not_cited",
    "parent_not_warranted",
    "wrong_type",
    "unnamed_reviewer",
    "material_not_linked",
)


def legal(from_state: str, to_state: str) -> bool:
    return to_state in EDGES.get(from_state, ())


def _ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Assessment:
    """What the rules make of one decision record."""

    obligations: List[Dict[str, Any]]
    admissions: Dict[int, Dict[str, str]]
    met: List[str]
    unmet: List[str]
    advisory_unmet: List[str]
    state: Optional[str]
    history: List[str] = field(default_factory=list)

    @property
    def warranted(self) -> bool:
        return self.state in AUTHORISING


def self_attested(item: Mapping[str, Any], actor_name: Optional[str]) -> bool:
    provider = item.get("provider")
    return not provider or provider == "self" or (actor_name is not None and provider == actor_name)


def admit(
    item: Mapping[str, Any],
    obligation: Mapping[str, Any],
    *,
    actor_name: Optional[str],
    at: Optional[datetime],
    parents: Mapping[str, Mapping[str, Any]],
) -> Tuple[bool, Optional[str]]:
    """Apply rules 1-6 to one item offered against one obligation. Returns ``(admitted, reason)``."""
    if self_attested(item, actor_name):
        return False, "self_attested"
    providers = obligation.get("providers") or []
    if providers and item.get("provider") not in providers:
        return False, "unqualified_provider"
    max_age = obligation.get("max_age_seconds")
    if max_age is not None:
        retrieved = _ts(item.get("retrieved_at"))
        if retrieved is None:
            return False, "no_timestamp"
        if at is not None and (at - retrieved).total_seconds() > max_age:
            return False, "stale"
    if not item.get("content_hash"):
        return False, "missing_digest"
    if item.get("type") == "record":
        parent = item.get("parent") or {}
        cited = parents.get(parent.get("record_id", ""))
        if cited is None or cited.get("hash") != parent.get("hash"):
            return False, "parent_not_cited"
        if cited.get("state") not in AUTHORISING:
            return False, "parent_not_warranted"
    if item.get("type") != obligation.get("requires") or (obligation.get("name") and item.get("name") != obligation.get("name")):
        return False, "wrong_type"
    return True, None


def human_linked(human: Optional[Mapping[str, Any]], digests: Iterable[str]) -> Tuple[bool, Optional[str]]:
    """Rule 7: a named reviewer, linked by digest to exactly the material they were shown."""
    human = human or {}
    if not human.get("reviewer"):
        return False, "unnamed_reviewer"
    shown = human.get("shown") or []
    known = set(digests)
    if not shown or any(d not in known for d in shown):
        return False, "material_not_linked"
    return True, None


def record_digests(record: Mapping[str, Any]) -> List[str]:
    """Every digest a reviewer could have been shown on this record."""
    out = [e.get("content_hash") for e in record.get("evidence") or [] if e.get("content_hash")]
    state_digest = (record.get("decision") or {}).get("state_digest")
    if state_digest:
        out.append(state_digest)
    return out


def assess(record: Mapping[str, Any], *, at: Optional[str] = None) -> Assessment:
    """Evaluate a decision record's obligations and derive the state its mandate and evidence allow.

    ``at`` is the verdict time for freshness; by default the record's ``verdict.at`` or timestamp.
    Items are judged only against the obligation they were offered for (``evidence[].obligation``).
    """
    actor_name = (record.get("actor") or {}).get("name")
    when = _ts(at or (record.get("verdict") or {}).get("at") or record.get("timestamp"))
    parents = {p["record_id"]: p for p in record.get("parents") or [] if isinstance(p, Mapping) and "record_id" in p}
    evidence = list(record.get("evidence") or [])
    digests = record_digests(record)

    admissions: Dict[int, Dict[str, str]] = {}
    obligations: List[Dict[str, Any]] = []
    met: List[str] = []
    unmet: List[str] = []
    advisory_unmet: List[str] = []
    for ob in record.get("obligations") or []:
        ob_out = dict(ob)
        satisfied: List[str] = []
        if ob.get("requires") == "human_review":
            ok, _ = human_linked(record.get("human"), digests)
            if ok:
                satisfied.append("human:" + str((record.get("human") or {}).get("reviewer")))
        for index, item in enumerate(evidence):
            if item.get("obligation") != ob.get("id"):
                continue
            admitted, reason = admit(item, ob, actor_name=actor_name, at=when, parents=parents)
            if admitted and ob.get("requires") == "human_review":
                admitted, reason = human_linked(record.get("human"), digests)
            admissions[index] = {"status": "admitted"} if admitted else {"status": "rejected", "reason": reason or ""}
            if admitted:
                satisfied.append(item.get("name", f"evidence[{index}]"))
        ob_out["met"] = bool(satisfied)
        ob_out["satisfied_by"] = satisfied
        obligations.append(ob_out)
        if satisfied:
            met.append(ob["id"])
        elif ob.get("kind") == "advisory":
            advisory_unmet.append(ob["id"])
        else:
            unmet.append(ob["id"])

    state, history = derive_state(record, unmet)
    return Assessment(obligations, admissions, met, unmet, advisory_unmet, state, history)


def derive_state(record: Mapping[str, Any], unmet: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """The state a decision reached within its own scope, and the path it took there.

    A record with neither obligations nor a verdict is an ordinary (L1) record and has no state.
    """
    if not record.get("obligations") and not record.get("verdict"):
        return None, []
    result = (record.get("mandate") or {}).get("result", "unchecked")
    status = (record.get("decision") or {}).get("status")
    if result == "deny":
        return "refused", ["proposed", "refused"]
    if result == "escalate":
        return "escalated", ["proposed", "escalated"]
    if unmet:
        return "pending_evidence", ["proposed", "pending_evidence"]
    if status == "acted":
        return "committed", ["proposed", "warranted", "committed"]
    return "warranted", ["proposed", "warranted"]


def check_history(history: Sequence[str]) -> Optional[str]:
    """A reason the path is illegal, or ``None``."""
    if not history:
        return None
    if history[0] != "proposed":
        return f"history starts at {history[0]!r}, not 'proposed'"
    for a, b in zip(history, history[1:]):
        if not legal(a, b):
            return f"illegal transition {a} -> {b}"
    return None


def check_transition(decision: Mapping[str, Any], current: str, transition: Mapping[str, Any]) -> Optional[str]:
    """A reason a later transition record is not allowed from ``current``, or ``None``."""
    verdict = transition.get("verdict") or {}
    to_state, from_state = verdict.get("state"), verdict.get("from_state")
    if from_state is not None and from_state != current:
        return f"transition claims to leave {from_state!r} but the decision is {current!r}"
    if not legal(current, to_state):
        return f"illegal transition {current} -> {to_state}"
    if to_state == "warranted" and current == "escalated":
        ok, reason = human_linked(transition.get("human"), record_digests(decision))
        if not ok:
            return f"a human decision to warrant needs a named reviewer linked to what they were shown ({reason})"
    if to_state == "warranted" and current == "pending_evidence":
        # Evidence cannot be added to a sealed record, so the only thing a later transition can
        # supply is a person: it may warrant a decision whose sole unmet obligations are human
        # sign-offs, and only with that human named and linked. Missing evidence of any other kind
        # needs a new decision that cites this one.
        unmet = set((decision.get("verdict") or {}).get("unmet") or [])
        kinds = {o.get("id"): o.get("requires") for o in decision.get("obligations") or []}
        other = sorted(ob for ob in unmet if kinds.get(ob) != "human_review")
        if other:
            return f"unmet obligations {', '.join(other)} need evidence, which a transition cannot add; record a new decision that cites this one"
        ok, reason = human_linked(transition.get("human"), record_digests(decision))
        if not ok:
            return f"a human sign-off needs a named reviewer linked to what they were shown ({reason})"
    if not verdict.get("decided_by"):
        return "a transition must say who decided it"
    return None
