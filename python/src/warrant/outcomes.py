"""Attach realised outcomes to past decisions, in bulk, from a file an operator can produce.

The realised result of a decision — an alert reopened, a loan defaulted, a promise to pay kept —
arrives days or months after the decision and lands in someone else's system. This module is the
bridge: a CSV keyed by subject or record id becomes linked ``outcome`` records, without mutating
anything. It is deliberately the dullest part of the product and the hardest to do without.

Two properties make it usable against a bank's operations team rather than its engineers:

* **Idempotent.** Outcome record ids are derived from the decision, the label and the observation
  time, so re-sending last week's file writes nothing new. A *changed* label is a new record, and
  the later one wins, because the ledger is append-only and corrections are additions.
* **Retroactive.** Nothing here cares whether the decision was recorded live or reconstructed by
  ``warrant import``, so eighteen months of history whose outcomes are already known can be joined
  in one pass. That is the difference between a reliability curve in week one and one in month six.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from warrant.ids import deterministic_ulid
from warrant.schema import SCHEMA_VERSION, ValidationError, validate

log = logging.getLogger("warrant.outcomes")

SUBJECT_COLUMNS = ("subject", "subject_ref")
RECORD_ID_COLUMNS = ("decision_record_id", "record_id")
REQUIRED_HINT = (
    "the file needs a header with a 'label' column and either 'subject' or 'decision_record_id'"
)


class OutcomeFileError(ValueError):
    """The outcome file cannot be read as outcomes. Raised before anything is written."""


@dataclass(frozen=True)
class OutcomeRow:
    """One line of an outcome file, already validated."""

    line: int
    label: str
    subject: Optional[str] = None
    decision_record_id: Optional[str] = None
    observed_at: Optional[str] = None
    score: Optional[float] = None
    source: Optional[str] = None

    @property
    def key(self) -> str:
        return self.decision_record_id or f"subject:{self.subject}"


@dataclass
class IngestReport:
    """What one ingest run read, matched and wrote."""

    files: int = 0
    rows: int = 0
    stream: Optional[str] = None
    written: int = 0
    duplicates: int = 0
    dry_run: bool = True
    matched_live: int = 0
    matched_imported: int = 0
    unmatched: List[Tuple[int, str]] = field(default_factory=list)
    invalid: List[Tuple[int, str]] = field(default_factory=list)
    by_label: Dict[str, int] = field(default_factory=dict)
    coverage: Optional["Coverage"] = None

    @property
    def matched(self) -> int:
        return self.matched_live + self.matched_imported

    def summary(self, max_listed: int = 10) -> str:
        lines = [f"read {self.rows} row(s) from {self.files} file(s)"]
        verb = (
            f"would write {self.matched} outcome record(s) (dry run)"
            if self.dry_run
            else f"wrote {self.written} outcome record(s)"
        )
        dup = f", {self.duplicates} already present" if self.duplicates else ""
        lines.append(f"{verb} to stream {self.stream!r}{dup}")
        if self.matched_imported:
            lines.append(
                f"  {self.matched_live} attached to live decisions, "
                f"{self.matched_imported} to imported ones"
            )
        for label, count in sorted(self.by_label.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {label}: {count}")
        if self.invalid:
            lines.append(f"  {len(self.invalid)} unusable row(s):")
            for line_no, reason in self.invalid[:max_listed]:
                lines.append(f"    line {line_no}: {reason}")
            if len(self.invalid) > max_listed:
                lines.append(f"    ... and {len(self.invalid) - max_listed} more")
        if self.unmatched:
            lines.append(f"  {len(self.unmatched)} row(s) matched no decision:")
            for line_no, key in self.unmatched[:max_listed]:
                lines.append(f"    line {line_no}: {key}")
            if len(self.unmatched) > max_listed:
                lines.append(f"    ... and {len(self.unmatched) - max_listed} more")
        if self.coverage is not None:
            lines.append("  " + self.coverage.summary())
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": self.files,
            "rows": self.rows,
            "stream": self.stream,
            "written": self.written,
            "duplicates": self.duplicates,
            "dry_run": self.dry_run,
            "matched": self.matched,
            "matched_live": self.matched_live,
            "matched_imported": self.matched_imported,
            "by_label": dict(self.by_label),
            "invalid": [{"line": n, "reason": r} for n, r in self.invalid],
            "unmatched": [{"line": n, "key": k} for n, k in self.unmatched],
            "coverage": self.coverage.to_dict() if self.coverage else None,
        }


@dataclass
class Coverage:
    """Outcome-attached share: the depth metric, and the one number worth reporting."""

    decisions: int = 0
    with_outcome: int = 0
    stream: Optional[str] = None
    by_class: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    @property
    def share(self) -> float:
        return self.with_outcome / self.decisions if self.decisions else 0.0

    def summary(self) -> str:
        where = f" in stream {self.stream!r}" if self.stream else ""
        head = (
            f"outcome-attached share{where}: {self.with_outcome}/{self.decisions} "
            f"({self.share:.1%})"
        )
        if len(self.by_class) <= 1:
            return head
        lines = [head]
        for cls, (total, attached) in sorted(self.by_class.items()):
            pct = attached / total if total else 0.0
            lines.append(f"    {cls}: {attached}/{total} ({pct:.1%})")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decisions": self.decisions,
            "with_outcome": self.with_outcome,
            "share": self.share,
            "stream": self.stream,
            "by_class": {
                cls: {"decisions": total, "with_outcome": attached, "share": attached / total if total else 0.0}
                for cls, (total, attached) in sorted(self.by_class.items())
            },
        }


def coverage(store: Any, *, stream: Optional[str] = None) -> Coverage:
    """How many decisions have an outcome attached, overall and per decision class.

    This is the depth metric behind every calibration claim: a reliability curve computed over a
    10% sample of decisions is a different statement from one over 90%, and the share belongs
    beside the curve wherever it is shown.
    """
    report = Coverage(stream=stream)
    for record in store.iter_records(stream):
        if record.get("record_type") != "decision":
            continue
        cls = record["decision"]["class"]
        total, attached = report.by_class.get(cls, (0, 0))
        report.decisions += 1
        has = store.latest_outcome(record["record_id"]) is not None
        if has:
            report.with_outcome += 1
        report.by_class[cls] = (total + 1, attached + (1 if has else 0))
    log.info(
        "outcome coverage: %d/%d decisions (%.1f%%) in stream %s",
        report.with_outcome,
        report.decisions,
        report.share * 100,
        stream,
    )
    return report


def read_outcome_file(path: Union[str, Path]) -> Tuple[List[OutcomeRow], List[Tuple[int, str]]]:
    """Parse one CSV into rows and per-line reasons for the ones that cannot be used.

    Raises :class:`OutcomeFileError` only for problems with the file as a whole — a missing header,
    no usable columns — so that a single bad line never costs the operator the whole run.
    """
    file_path = Path(path)
    try:
        handle = file_path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OutcomeFileError(f"{file_path}: cannot be read ({exc.strerror})") from exc

    rows: List[OutcomeRow] = []
    invalid: List[Tuple[int, str]] = []
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise OutcomeFileError(f"{file_path}: file is empty. {REQUIRED_HINT}")
        columns = {(name or "").strip().lower() for name in reader.fieldnames}
        subject_col = next((c for c in SUBJECT_COLUMNS if c in columns), None)
        record_col = next((c for c in RECORD_ID_COLUMNS if c in columns), None)
        if "label" not in columns:
            raise OutcomeFileError(f"{file_path}: no 'label' column. {REQUIRED_HINT}")
        if subject_col is None and record_col is None:
            raise OutcomeFileError(f"{file_path}: no subject or decision_record_id column. {REQUIRED_HINT}")
        for line_no, raw in enumerate(reader, start=2):
            cells = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            try:
                rows.append(_row_from_cells(cells, line_no, subject_col, record_col))
            except OutcomeFileError as exc:
                invalid.append((line_no, str(exc)))
    log.info("read %d outcome row(s) from %s, %d unusable", len(rows), file_path, len(invalid))
    return rows, invalid


def _row_from_cells(
    cells: Dict[str, Any], line_no: int, subject_col: Optional[str], record_col: Optional[str]
) -> OutcomeRow:
    label = cells.get("label") or ""
    if not label:
        raise OutcomeFileError("empty label")
    subject = cells.get(subject_col) if subject_col else None
    record_id = cells.get(record_col) if record_col else None
    if not subject and not record_id:
        raise OutcomeFileError("neither subject nor decision_record_id is set")
    observed_at = cells.get("observed_at") or None
    if observed_at is not None:
        observed_at = _as_utc(observed_at)
    score_cell = cells.get("score")
    score: Optional[float] = None
    if score_cell not in (None, ""):
        try:
            score = float(score_cell)
        except (TypeError, ValueError) as exc:
            raise OutcomeFileError(f"score {score_cell!r} is not a number") from exc
    return OutcomeRow(
        line=line_no,
        label=label,
        subject=subject or None,
        decision_record_id=record_id or None,
        observed_at=observed_at,
        score=score,
        source=cells.get("source") or None,
    )


def _as_utc(value: str) -> str:
    """Normalise a timestamp cell to the RFC 3339 form the schema wants."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeFileError(f"observed_at {value!r} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _epoch_ms(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def build_outcome_record(decision: Dict[str, Any], row: OutcomeRow, *, default_source: Optional[str] = None) -> Dict[str, Any]:
    """An unsealed ``outcome`` record linked to ``decision``, with a deterministic id.

    The id derives from the decision, the label and the observation time, so the same file
    ingested twice writes the same ids and the store's duplicate check absorbs the second run.
    A corrected label produces a different id and lands as a later record; both survive.
    """
    observed_at = row.observed_at or decision["timestamp"]
    outcome: Dict[str, Any] = {"status": "observed", "label": row.label, "observed_at": observed_at}
    if row.score is not None:
        outcome["score"] = row.score
    source = row.source or default_source
    if source:
        outcome["source"] = source
    key = f"{decision['record_id']}|outcome|{row.label}|{observed_at}"
    record = {
        "record_id": deterministic_ulid(_epoch_ms(observed_at), key),
        "record_type": "outcome",
        "tenant": decision["tenant"],
        "stream": decision["stream"],
        "timestamp": observed_at,
        "schema_version": SCHEMA_VERSION,
        "origin": decision.get("origin", "live"),
        "references": {"decision_record_id": decision["record_id"]},
        "outcome": outcome,
    }
    validate(record)
    return record


def ingest_outcomes(
    paths: Sequence[Union[str, Path]],
    store: Any,
    *,
    stream: str,
    source: Optional[str] = None,
    dry_run: bool = False,
    with_coverage: bool = True,
) -> IngestReport:
    """Read outcome files and attach each row to the decision it names.

    ``stream`` scopes subject lookups; rows carrying ``decision_record_id`` are matched directly.
    Rows that match nothing are collected and reported rather than raised: an operator's export
    routinely contains subjects from outside the window, and that is a finding, not a failure.
    """
    if not paths:
        raise ValueError("no outcome files given")
    report = IngestReport(files=len(paths), stream=stream, dry_run=dry_run)
    pending: List[Dict[str, Any]] = []
    for path in paths:
        rows, invalid = read_outcome_file(path)
        report.rows += len(rows) + len(invalid)
        report.invalid.extend(invalid)
        for row in rows:
            decision = _find_decision(store, row, stream)
            if decision is None:
                report.unmatched.append((row.line, row.key))
                continue
            if decision.get("origin") == "imported":
                report.matched_imported += 1
            else:
                report.matched_live += 1
            try:
                pending.append(build_outcome_record(decision, row, default_source=source))
            except ValidationError as exc:
                report.invalid.append((row.line, f"would not validate: {'; '.join(exc.errors)}"))
                continue
            report.by_label[row.label] = report.by_label.get(row.label, 0) + 1

    if not dry_run and pending:
        before = store.count(stream)
        store.write(pending)
        after = store.count(stream)
        report.written = after - before
        report.duplicates = len(pending) - report.written
        log.info(
            "ingested %d outcome record(s) into stream %s, %d already present",
            report.written,
            stream,
            report.duplicates,
        )
    if with_coverage and not dry_run:
        report.coverage = coverage(store, stream=stream)
    return report


def _find_decision(store: Any, row: OutcomeRow, stream: str) -> Optional[Dict[str, Any]]:
    if row.decision_record_id:
        record = store.get(row.decision_record_id)
        if record is None or record.get("record_type") != "decision":
            return None
        return record
    found = store.find_decision(stream, row.subject)
    return store.get(found) if found else None


def iter_joined(store: Any, stream: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """Every decision in the stream with its latest outcome merged in under ``outcome``.

    The same join ``warrant set create`` uses, exposed for calibration so the two cannot drift
    on what "the outcome of a decision" means.
    """
    for record in store.iter_records(stream):
        if record.get("record_type") != "decision":
            continue
        outcome = store.latest_outcome(record["record_id"])
        yield dict(record, outcome=outcome["outcome"]) if outcome is not None else record
