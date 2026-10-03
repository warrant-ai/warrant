"""Offline verification of exported record chains, and the ADR conformance level each reaches.

Without a key set this is the 0.1 check: sequence, hash chain, schema, unique ids. Given a key set it
also checks issuer signatures; given parent exports it checks cross-organisation handoffs; given
checkpoints it checks witnessed commitments. Each chain is reported with the highest level it
reaches (ADR 7) and the reason it stops there:

* **L1 Recorded**: chained, schema-valid and signed by a key valid at each record's time.
* **L2 Warranted**: L1, every decision carries a verdict that the admissibility rules reproduce,
  no decision acted without a warrant, and every lifecycle path is legal.
* **L3 Attested**: L2, and the chain head is covered by an issuer-signed checkpoint co-signed by at
  least one witness whose key belongs to a different issuer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from warrant.hashing import record_hash, seal_matches
from warrant.schema import ValidationError, validate

MAX_ERRORS_PER_STREAM = 20
LEVELS = ("below L1", "L1", "L2", "L3")


@dataclass
class StreamReport:
    stream: str
    records: int
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    level: str = "below L1"
    level_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def _add(report: StreamReport, msg: str, *, warning: bool = False) -> None:
    target = report.warnings if warning else report.errors
    if len(target) < MAX_ERRORS_PER_STREAM:
        target.append(msg)


def verify_records(
    records: Iterable[Dict[str, Any]],
    *,
    keyring: Any = None,
    parent_records: Optional[Iterable[Dict[str, Any]]] = None,
    checkpoints: Sequence[Mapping[str, Any]] = (),
) -> List[StreamReport]:
    """Check every chain's sequence, hashes, schema conformance and id uniqueness, and ADR level.

    Chains are per tenant and stream, reported as ``tenant/stream``. Records may arrive in any
    order; each chain is sorted by ``sequence`` first.
    """
    by_stream: Dict[str, List[Dict[str, Any]]] = {}
    unstreamed: List[str] = []
    for record in records:
        stream = record.get("stream")
        tenant = record.get("tenant")
        if isinstance(stream, str) and stream and isinstance(tenant, str) and tenant:
            by_stream.setdefault(f"{tenant}/{stream}", []).append(record)
        else:
            unstreamed.append(str(record.get("record_id", "?")))

    reports: List[StreamReport] = []
    if unstreamed:
        reports.append(StreamReport("(no stream)", len(unstreamed), [f"records without a stream: {', '.join(unstreamed[:5])}"]))

    parents_by_id: Dict[str, Dict[str, Any]] = {}
    for parent in parent_records or ():
        if isinstance(parent, dict) and parent.get("record_id"):
            parents_by_id[parent["record_id"]] = parent

    seen_ids: Dict[str, str] = {}
    for stream in sorted(by_stream):
        items = by_stream[stream]
        report = StreamReport(stream, len(items))
        reports.append(report)

        if any(not isinstance(r.get("sequence"), int) for r in items):
            _add(report, "one or more records lack an integer sequence; cannot establish chain order")
            report.level_reason = "chain order cannot be established"
            continue
        items.sort(key=lambda r: r["sequence"])
        _check_chain(report, items, seen_ids, stream)
        signed = _check_signatures(report, items, keyring)
        adr_ok = _check_adr(report, items, parents_by_id, keyring)
        attested = _check_checkpoints(report, items, keyring, checkpoints, stream)
        _assign_level(report, signed, adr_ok, attested, items)
    return reports


def _check_chain(report: StreamReport, items: List[Dict[str, Any]], seen_ids: Dict[str, str], stream: str) -> None:
    prev_hash = None
    for expected, record in enumerate(items, start=1):
        rid = record.get("record_id", "?")
        seq = record["sequence"]
        if seq != expected:
            _add(report, f"#{expected}: expected sequence {expected}, found {seq} (record {rid})")
        seal = record.get("seal")
        if not isinstance(seal, dict) or "hash" not in seal:
            _add(report, f"#{seq}: record {rid} has no seal")
            prev_hash = None
            continue
        if seal.get("prev_hash") != prev_hash:
            _add(report, f"#{seq}: record {rid} prev_hash does not match the previous record's hash")
        try:
            computed = record_hash(record, seal.get("prev_hash"))
        except ValueError as exc:
            _add(report, f"#{seq}: record {rid} cannot be checked: {exc}")
        else:
            if seal["hash"] != computed:
                _add(report, f"#{seq}: record {rid} hash mismatch; the body was altered after sealing")
        try:
            validate(record)
        except ValidationError as exc:
            _add(report, f"#{seq}: record {rid} fails schema: {exc.errors[0]}")
        if rid in seen_ids:
            _add(report, f"#{seq}: record_id {rid} already appeared in stream {seen_ids[rid]}")
        seen_ids[rid] = stream
        prev_hash = seal["hash"]


def _check_signatures(report: StreamReport, items: List[Dict[str, Any]], keyring: Any) -> Optional[str]:
    """The issuer name if every record is validly signed by one issuer; otherwise ``None``."""
    unsigned = [r for r in items if not (r.get("seal") or {}).get("signature")]
    if keyring is None:
        if len(unsigned) < len(items):
            _add(report, "records are signed but no key set was given; signatures not checked (pass --keys)", warning=True)
        return None
    from warrant.signing import verify_seal

    issuers = set()
    bad = 0
    for record in items:
        if not (record.get("seal") or {}).get("signature"):
            continue
        ok, detail = verify_seal(record, keyring)
        if ok:
            issuers.add(detail)
        else:
            bad += 1
            _add(report, f"#{record.get('sequence')}: record {record.get('record_id')} signature: {detail}")
    if unsigned:
        _add(report, f"{len(unsigned)} of {len(items)} record(s) are unsigned", warning=True)
    if len(issuers) > 1:
        _add(report, f"one chain signed by several issuers: {', '.join(sorted(issuers))}")
        return None
    if bad or unsigned or not issuers:
        return None
    return issuers.pop()


def _check_adr(report: StreamReport, items: List[Dict[str, Any]], parents_by_id: Dict[str, Dict[str, Any]], keyring: Any) -> bool:
    """Recompute every verdict, check lifecycle paths and cited parents. True if nothing failed."""
    from warrant.admissibility import AUTHORISING, assess, check_history, check_transition

    findings = []

    def finding(msg: str) -> None:
        findings.append(msg)
        _add(report, "L2: " + msg, warning=True)

    decisions: Dict[str, Dict[str, Any]] = {}
    state: Dict[str, str] = {}
    for record in items:
        rid = record.get("record_id")
        seq = record.get("sequence")
        rtype = record.get("record_type")
        if rtype == "decision":
            decisions[rid] = record
            verdict = record.get("verdict")
            if not verdict:
                continue
            recorded = verdict.get("state")
            assessment = assess(record)
            if assessment.state is not None and assessment.state != recorded:
                finding(f"#{seq}: record {rid} records state {recorded!r} but its obligations and evidence give {assessment.state!r}")
            if sorted(verdict.get("unmet") or []) != sorted(assessment.unmet):
                finding(f"#{seq}: record {rid} unmet obligations {verdict.get('unmet') or []} do not match the rules ({assessment.unmet})")
            for index, admission in assessment.admissions.items():
                written = ((record.get("evidence") or [])[index].get("admission") or {})
                if written and written.get("status") != admission["status"]:
                    finding(f"#{seq}: record {rid} evidence {index} is marked {written.get('status')} but the rules say {admission['status']}")
            history = [h.get("state") for h in verdict.get("history") or []]
            problem = check_history(history)
            if problem:
                finding(f"#{seq}: record {rid} {problem}")
            elif history and history[-1] != recorded:
                finding(f"#{seq}: record {rid} history ends at {history[-1]!r} but the state is {recorded!r}")
            status = (record.get("decision") or {}).get("status")
            if status == "acted" and recorded != "committed":
                finding(f"#{seq}: record {rid} acted without a warrant (state {recorded!r})")
            state[rid] = recorded or "proposed"
            for msg in _check_parents(record, parents_by_id, keyring):
                finding(msg)
        elif rtype == "transition":
            target = (record.get("references") or {}).get("decision_record_id")
            decision = decisions.get(target)
            if decision is None:
                finding(f"#{seq}: transition {rid} refers to decision {target}, which is not earlier in this chain")
                continue
            problem = check_transition(decision, state.get(target, "proposed"), record)
            if problem:
                finding(f"#{seq}: transition {rid}: {problem}")
            else:
                state[target] = (record.get("verdict") or {}).get("state")
    return not findings


def _parent_state_at(parent: Mapping[str, Any], family: Iterable[Mapping[str, Any]], at: str) -> Optional[str]:
    current = (parent.get("verdict") or {}).get("state")
    for linked in sorted(family, key=lambda r: r.get("sequence") or 0):
        if linked.get("record_type") == "transition" and (linked.get("references") or {}).get("decision_record_id") == parent.get("record_id"):
            if linked.get("timestamp", "") <= at:
                current = (linked.get("verdict") or {}).get("state", current)
    return current


def _check_parents(record: Mapping[str, Any], parents_by_id: Dict[str, Dict[str, Any]], keyring: Any) -> List[str]:
    """Problems with the upstream records this decision cites; an unsupplied parent is one of them."""
    from warrant.admissibility import AUTHORISING

    out: List[str] = []
    rid = record.get("record_id")
    for cited in record.get("parents") or []:
        pid = cited.get("record_id")
        parent = parents_by_id.get(pid)
        if parent is None:
            out.append(f"record {rid} cites {pid}, which was not supplied (pass the issuer's export with --parents)")
            continue
        seal = parent.get("seal") or {}
        if seal.get("hash") != cited.get("hash"):
            out.append(f"record {rid} cites {pid} with hash {str(cited.get('hash'))[:12]}, but the parent's seal is {str(seal.get('hash'))[:12]}")
            continue
        if not seal_matches(parent):
            out.append(f"record {rid} cites {pid}, which was altered after its issuer sealed it")
            continue
        if keyring is not None:
            from warrant.signing import verify_seal

            ok, detail = verify_seal(parent, keyring)
            if not ok:
                out.append(f"record {rid} cites {pid}: {detail}")
                continue
            if cited.get("issuer") and cited["issuer"] != detail:
                out.append(f"record {rid} names {cited['issuer']} as issuer of {pid}, but it is signed by {detail}")
        actual = _parent_state_at(parent, parents_by_id.values(), record.get("timestamp", ""))
        if cited.get("state") in AUTHORISING and actual not in AUTHORISING:
            out.append(f"record {rid} relied on {pid} as {cited.get('state')}, but it was {actual!r} at the time")
    return out


def _check_checkpoints(report: StreamReport, items: List[Dict[str, Any]], keyring: Any, checkpoints: Sequence[Mapping[str, Any]], stream: str) -> bool:
    """True if some valid checkpoint covers the head and carries an independent witness."""
    if not checkpoints:
        return False
    from warrant.checkpoint import CheckpointError, verify_checkpoint

    tenant, _, name = stream.partition("/")
    attested = False
    for cp in checkpoints:
        if cp.get("tenant") != tenant or cp.get("stream") != name:
            continue
        if keyring is None:
            _add(report, "a checkpoint was given without a key set; it cannot be checked", warning=True)
            return False
        try:
            result = verify_checkpoint(cp, keyring, records=items)
        except CheckpointError as exc:
            _add(report, f"checkpoint at size {cp.get('tree_size')}: {exc}")
            continue
        if result.tree_size != len(items):
            _add(report, f"checkpoint covers {result.tree_size} of {len(items)} records; the head is not attested", warning=True)
            continue
        if result.independent_witnesses:
            attested = True
        else:
            _add(report, "the checkpoint covering the head has no co-signature from an independent witness", warning=True)
    return attested


def _assign_level(report: StreamReport, signed_by: Optional[str], adr_ok: bool, attested: bool, items: List[Dict[str, Any]]) -> None:
    if report.errors:
        report.level, report.level_reason = "below L1", "the chain has errors"
        return
    if signed_by is None:
        report.level, report.level_reason = "below L1", "not every record is signed by a key in the given key set"
        return
    decisions = [r for r in items if r.get("record_type") == "decision"]
    if not decisions or any(not r.get("verdict") for r in decisions) or not adr_ok:
        report.level = "L1"
        report.level_reason = "not every decision carries a verdict the rules reproduce" if decisions else "the chain holds no decisions"
        return
    if not attested:
        report.level, report.level_reason = "L2", "no checkpoint covering the head is co-signed by an independent witness"
        return
    report.level, report.level_reason = "L3", ""


def level_at_least(level: str, required: str) -> bool:
    return LEVELS.index(level) >= LEVELS.index(required)
