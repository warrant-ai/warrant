"""Offline verification of exported record chains."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from warrant.hashing import record_hash
from warrant.schema import ValidationError, validate

MAX_ERRORS_PER_STREAM = 20


@dataclass
class StreamReport:
    stream: str
    records: int
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_records(records: Iterable[Dict[str, Any]]) -> List[StreamReport]:
    """Check every chain's sequence, hashes, schema conformance and id uniqueness.

    Chains are per tenant and stream, reported as ``tenant/stream``. Records may
    arrive in any order; each chain is sorted by ``sequence`` first.
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

    seen_ids: Dict[str, str] = {}
    for stream in sorted(by_stream):
        items = by_stream[stream]
        report = StreamReport(stream, len(items))
        reports.append(report)

        def add(msg: str) -> None:
            if len(report.errors) < MAX_ERRORS_PER_STREAM:
                report.errors.append(msg)

        if any(not isinstance(r.get("sequence"), int) for r in items):
            add("one or more records lack an integer sequence; cannot establish chain order")
            continue
        items.sort(key=lambda r: r["sequence"])
        prev_hash = None
        for expected, record in enumerate(items, start=1):
            rid = record.get("record_id", "?")
            seq = record["sequence"]
            if seq != expected:
                add(f"#{expected}: expected sequence {expected}, found {seq} (record {rid})")
            seal = record.get("seal")
            if not isinstance(seal, dict) or "hash" not in seal:
                add(f"#{seq}: record {rid} has no seal")
                prev_hash = None
                continue
            if seal.get("prev_hash") != prev_hash:
                add(f"#{seq}: record {rid} prev_hash does not match the previous record's hash")
            computed = record_hash(record, seal.get("prev_hash"))
            if seal["hash"] != computed:
                add(f"#{seq}: record {rid} hash mismatch; the body was altered after sealing")
            try:
                validate(record)
            except ValidationError as exc:
                add(f"#{seq}: record {rid} fails schema: {exc.errors[0]}")
            if rid in seen_ids:
                add(f"#{seq}: record_id {rid} already appeared in stream {seen_ids[rid]}")
            seen_ids[rid] = stream
            prev_hash = seal["hash"]
    return reports
