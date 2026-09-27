"""Walk a decision's parent links across organisations' exports and show where it went wrong.

This is the worked example's "a verifier walks the parent links in minutes": given the bank's
export and the partner's, start at the bank's decision and check every upstream step: is the record
intact, signed by its issuer, is its verdict what its evidence supports, and was each parent it
relied on warranted at the time. Steps are listed upstream first, so the first failing step is the
earliest point in the pipeline where the records show a problem.

Locating the step is not assigning liability; the contract between the parties decides who pays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from warrant.admissibility import AUTHORISING, assess
from warrant.hashing import record_hash


@dataclass
class Step:
    record_id: str
    depth: int
    issuer: str
    decision_class: str
    state: Optional[str]
    signed_by: Optional[str] = None
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    parents: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    def describe(self) -> str:
        head = f"{'  ' * self.depth}{'FAIL' if self.problems else 'ok  '} {self.record_id} {self.issuer} {self.decision_class} state={self.state or '-'}"
        if self.signed_by:
            head += f" signed-by={self.signed_by}"
        lines = [head]
        lines += [f"{'  ' * self.depth}     - {p}" for p in self.problems]
        lines += [f"{'  ' * self.depth}     . {n}" for n in self.notes]
        return "\n".join(lines)


def trace(record_id: str, records: Iterable[Mapping[str, Any]], keyring: Any = None) -> List[Step]:
    """Every step from ``record_id`` upstream, deepest first. Raises ``LookupError`` if it is absent."""
    by_id: Dict[str, Mapping[str, Any]] = {}
    for record in records:
        if isinstance(record, Mapping) and record.get("record_id"):
            by_id[record["record_id"]] = record
    if record_id not in by_id:
        raise LookupError(f"record {record_id} is not in the exports given")
    steps: List[Step] = []
    seen = set()

    def visit(rid: str, depth: int) -> None:
        if rid in seen:
            return
        seen.add(rid)
        record = by_id[rid]
        step = Step(
            record_id=rid, depth=depth, issuer=str(record.get("tenant")),
            decision_class=str((record.get("decision") or {}).get("class", record.get("record_type"))),
            state=(record.get("verdict") or {}).get("state"),
        )
        seal = record.get("seal") or {}
        if not seal.get("hash") or record_hash(record, seal.get("prev_hash")) != seal.get("hash"):
            step.problems.append("the record does not match its seal: altered after sealing")
        if keyring is not None:
            from warrant.signing import verify_seal

            ok, detail = verify_seal(record, keyring)
            if ok:
                step.signed_by = detail
            else:
                step.problems.append(f"signature: {detail}")
        else:
            step.notes.append("signature not checked (no key set given)")
        if record.get("obligations"):
            assessment = assess(record)
            if assessment.state != step.state:
                step.problems.append(f"records state {step.state!r} but its evidence supports {assessment.state!r}")
            for ob in assessment.unmet:
                step.problems.append(f"obligation {ob} unmet")
            evidence = record.get("evidence") or []
            for index, admission in sorted(assessment.admissions.items()):
                if admission["status"] == "rejected":
                    step.notes.append(f"evidence {evidence[index].get('name')!r} rejected: {admission.get('reason')}")
            if (record.get("decision") or {}).get("status") == "acted" and not assessment.warranted:
                step.problems.append("acted without a warrant")
        for cited in record.get("parents") or []:
            pid = cited.get("record_id")
            step.parents.append(pid)
            parent = by_id.get(pid)
            if parent is None:
                step.problems.append(f"relies on {pid}, which is in none of the exports given")
                continue
            if (parent.get("seal") or {}).get("hash") != cited.get("hash"):
                step.problems.append(f"cites {pid} by a hash its issuer's record does not have")
            if cited.get("state") in AUTHORISING and (parent.get("verdict") or {}).get("state") not in AUTHORISING:
                step.problems.append(f"relied on {pid} as {cited.get('state')}, but its issuer recorded {(parent.get('verdict') or {}).get('state')!r}")
            visit(pid, depth + 1)
        steps.append(step)

    visit(record_id, 0)
    steps.sort(key=lambda s: -s.depth)
    return steps
