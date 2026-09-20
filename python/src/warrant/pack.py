"""Assemble an evidence pack: the records, the proof they are intact, and what they add up to.

This is the artefact a model validation committee, an internal audit function or a supervisor
actually reads, and the one a customer renews for. It is also the exit path: the RBI's outsourcing
directions require a documented way out, and an evidence store a bank cannot extract without its
vendor is a store the bank cannot rely on. So the raw pack is open-source and verifiable with
nothing but the ``warrant`` CLI.

Two rules the format depends on:

* **A pack that does not verify is never written.** The chain is checked before anything reaches
  disk, and a failure aborts with the reason. Shipping an evidence pack that fails its own
  verification would be worse than shipping none.
* **The pack states what it does not prove.** A hash chain shows the records have not been altered
  since they were sealed relative to one another. It does not show that whoever operates the store
  could not have re-sealed the whole chain, and per-writer signing is not built. Saying so first is
  what makes the rest of the document credible.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from warrant import __version__
from warrant.outcomes import Coverage, coverage
from warrant.schema import SCHEMA_VERSION
from warrant.verify import verify_records

log = logging.getLogger("warrant.pack")


class PackError(RuntimeError):
    """The pack cannot be produced. Raised before anything is written."""


@dataclass
class Manifest:
    """Provenance for one pack: what it covers, and what produced it."""

    tenant: Optional[str] = None
    stream: Optional[str] = None
    generated_at: str = ""
    warrant_version: str = __version__
    schema_version: str = SCHEMA_VERSION
    records: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    first_outcome_observed: Optional[str] = None
    last_outcome_observed: Optional[str] = None
    chain_head_hash: Optional[str] = None
    chain_head_sequence: Optional[int] = None
    policies: Dict[str, int] = field(default_factory=dict)
    question_sets: Dict[str, int] = field(default_factory=dict)
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant": self.tenant,
            "stream": self.stream,
            "generated_at": self.generated_at,
            "warrant_version": self.warrant_version,
            "schema_version": self.schema_version,
            "records": self.records,
            "by_type": dict(sorted(self.by_type.items())),
            "first_decision": self.first_timestamp,
            "last_decision": self.last_timestamp,
            "first_outcome_observed": self.first_outcome_observed,
            "last_outcome_observed": self.last_outcome_observed,
            "chain_head_hash": self.chain_head_hash,
            "chain_head_sequence": self.chain_head_sequence,
            "policies": dict(sorted(self.policies.items())),
            "question_sets": dict(sorted(self.question_sets.items())),
            "verified": self.verified,
        }


def _describe(records: List[Dict[str, Any]]) -> Manifest:
    manifest = Manifest(generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    manifest.records = len(records)
    tenants = set()
    streams = set()
    for record in records:
        tenants.add(record.get("tenant"))
        streams.add(record.get("stream"))
        kind = record.get("record_type", "unknown")
        manifest.by_type[kind] = manifest.by_type.get(kind, 0) + 1
        stamp = record.get("timestamp")
        if stamp and kind == "decision":
            if manifest.first_timestamp is None or stamp < manifest.first_timestamp:
                manifest.first_timestamp = stamp
            if manifest.last_timestamp is None or stamp > manifest.last_timestamp:
                manifest.last_timestamp = stamp
        observed = (record.get("outcome") or {}).get("observed_at") if kind == "outcome" else None
        if observed:
            if manifest.first_outcome_observed is None or observed < manifest.first_outcome_observed:
                manifest.first_outcome_observed = observed
            if manifest.last_outcome_observed is None or observed > manifest.last_outcome_observed:
                manifest.last_outcome_observed = observed
        mandate = record.get("mandate") or {}
        if mandate.get("policy_id"):
            key = f"{mandate['policy_id']}@{mandate.get('policy_version', '?')}"
            manifest.policies[key] = manifest.policies.get(key, 0) + 1
        question_set = (record.get("decision") or {}).get("question_set")
        if question_set:
            key = f"{question_set['id']}@{question_set['version']}"
            manifest.question_sets[key] = manifest.question_sets.get(key, 0) + 1
    manifest.tenant = tenants.pop() if len(tenants) == 1 else None
    manifest.stream = streams.pop() if len(streams) == 1 else None
    head = max(records, key=lambda r: (r.get("sequence") or 0), default=None)
    if head is not None:
        manifest.chain_head_hash = (head.get("seal") or {}).get("hash")
        manifest.chain_head_sequence = head.get("sequence")
    return manifest


def _front_page(
    manifest: Manifest,
    cover: Coverage,
    calibration: Optional[Any],
    title: Optional[str],
    policies_copied: List[str],
    questions_written: Optional[List[str]] = None,
) -> str:
    name = title or f"Decision evidence pack — {manifest.stream or 'all streams'}"
    span = (
        f"{manifest.first_timestamp} to {manifest.last_timestamp}"
        if manifest.first_timestamp
        else "no decisions"
    )
    lines = [
        f"# {name}",
        "",
        f"Generated {manifest.generated_at} by warrant {manifest.warrant_version}, "
        f"decision record schema v{manifest.schema_version}.",
        "",
        "## What this covers",
        "",
        f"- Tenant: `{manifest.tenant or 'several'}`",
        f"- Stream: `{manifest.stream or 'several'}`",
        f"- Decisions made: {span}",
        f"- Records: {manifest.records}"
        + (" (" + ", ".join(f"{n} {k}" for k, n in sorted(manifest.by_type.items())) + ")" if manifest.by_type else ""),
    ]
    if manifest.first_outcome_observed:
        lines.append(
            f"- Outcomes observed: {manifest.first_outcome_observed} to {manifest.last_outcome_observed}"
        )
    if manifest.policies:
        lines.append("- Policy versions in force: " + ", ".join(f"`{k}` ({n})" for k, n in sorted(manifest.policies.items())))
    if manifest.question_sets:
        lines.append("- Question sets: " + ", ".join(f"`{k}` ({n})" for k, n in sorted(manifest.question_sets.items())))

    lines += [
        "",
        "## Verify it yourself, offline",
        "",
        "Nothing in this pack needs our servers, our software licence or our cooperation:",
        "",
        "```",
        'pip install "warrantai"',
        "warrant verify records.jsonl",
        "```",
        "",
        "That re-computes every record's hash and walks the chain from first to last. The chain "
        f"head at the time of export was sequence {manifest.chain_head_sequence} with hash "
        f"`{manifest.chain_head_hash}`; compare it against the value in `manifest.json` and "
        "against any earlier pack you hold.",
        "",
        "## What this proves, and what it does not",
        "",
        "Each record's hash covers its own contents, and each carries the hash of the record "
        "before it. Altering, removing or reordering a single record breaks every link after it, "
        "so the pack shows that these records have not been changed since they were sealed.",
        "",
        "It does **not** show that the operator of the store could not have re-sealed the whole "
        "chain and produced a different but internally consistent history. That requires "
        "per-writer signing and periodic external anchoring, neither of which is implemented in "
        "this version. An evidence store whose operator could rewrite it without detection is "
        "not fully independent evidence, and this pack says so rather than implying otherwise.",
        "",
        "## Outcome coverage",
        "",
        f"{cover.summary()}",
        "",
        "A decision without a realised outcome attached cannot contribute to any measurement of "
        "whether the decider performed as claimed. The share above is the base every figure in "
        "this pack rests on.",
    ]

    if calibration is not None:
        lines += [
            "",
            "## Calibration",
            "",
            f"Correct is defined as: `{calibration.correct_when}`",
            "",
        ]
        if calibration.where:
            lines += [f"Computed over decisions where `{calibration.where}`.", ""]
        if not calibration.usable:
            lines.append(
                "No decision in this period carries both a stated confidence and a realised "
                "outcome, so no reliability curve can be computed. This is a statement about "
                "coverage, not about accuracy."
            )
        else:
            lines += [
                f"Over {calibration.usable} decisions with both halves, the decider stated an "
                f"average confidence of {calibration.mean_confidence:.3f} and was correct "
                f"{calibration.accuracy:.1%} of the time.",
                "",
                f"- Expected Calibration Error: **{calibration.ece:.4f}**",
                f"- Maximum Calibration Error (worst band): **{calibration.mce:.4f}**",
                f"- Brier score: {calibration.brier:.4f}",
                "",
                "| Confidence band | Decisions | Stated | Observed | Gap |",
                "|---|---:|---:|---:|---:|",
            ]
            for bucket in calibration.buckets:
                if not bucket.count:
                    continue
                lines.append(
                    f"| {bucket.low:.2f}–{bucket.high:.2f} | {bucket.count} | "
                    f"{bucket.mean_confidence:.3f} | {bucket.observed_rate:.3f} | {bucket.gap:+.3f} |"
                )
            lines += [
                "",
                "A negative gap means the decider was less accurate than it claimed in that band. "
                "The bands being automated are the ones to read first.",
            ]
            if calibration.breakdown:
                lines += [
                    "",
                    f"### By {calibration.dimension}",
                    "",
                    f"| {calibration.dimension} | Decisions | Stated | Observed | ECE |",
                    "|---|---:|---:|---:|---:|",
                ]
                for key, sub in sorted(calibration.breakdown.items()):
                    lines.append(
                        f"| {key} | {sub.usable} | {sub.mean_confidence:.3f} | "
                        f"{sub.accuracy:.3f} | {sub.ece:.4f} |"
                    )
                lines.append("")
                lines.append(
                    "A single acceptable overall figure can hide a segment that is not. This "
                    "breakdown exists so that cannot pass unnoticed."
                )

    lines += ["", "## Files", "", "| File | Contents |", "|---|---|"]
    lines.append("| `records.jsonl` | Every sealed record in the period, one JSON object per line |")
    lines.append("| `manifest.json` | Provenance: period, counts, chain head, versions |")
    lines.append("| `coverage.json` | Outcome-attached share, overall and per decision class |")
    if calibration is not None:
        lines.append("| `calibration.json` | The reliability curve and its inputs, machine-readable |")
    for name_ in policies_copied:
        lines.append(f"| `policies/{name_}` | Policy text as it applied in this period |")
    for name_ in questions_written or []:
        lines.append(
            f"| `questions/{name_}` | The questions these decisions were made by answering, at that version |"
        )
    lines.append("")
    return "\n".join(lines)


@dataclass
class PackResult:
    """Where the pack went and what it contains."""

    directory: Path
    manifest: Manifest
    coverage: Coverage
    calibration: Optional[Any] = None
    files: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"evidence pack written to {self.directory}"]
        lines.append(
            f"  {self.manifest.records} record(s), decisions "
            f"{self.manifest.first_timestamp} to {self.manifest.last_timestamp}, chain verified"
        )
        lines.append("  " + self.coverage.summary())
        if self.calibration is not None and self.calibration.usable:
            lines.append(
                f"  calibration: {self.calibration.usable} usable, "
                f"ECE {self.calibration.ece:.4f}, MCE {self.calibration.mce:.4f}"
            )
        lines.append("  " + ", ".join(self.files))
        lines.append(f"  verify with: warrant verify {self.directory / 'records.jsonl'}")
        return "\n".join(lines)


def build_pack(
    store: Any,
    directory: Path,
    *,
    stream: Optional[str] = None,
    policy_dir: Optional[Path] = None,
    questions_dir: Optional[Path] = None,
    correct_when: Optional[str] = None,
    answer: Optional[str] = None,
    by: Optional[str] = None,
    where: Optional[str] = None,
    buckets: int = 10,
    title: Optional[str] = None,
) -> PackResult:
    """Write an evidence pack for ``stream`` into ``directory``.

    The chain is verified before anything is written; a pack that fails its own verification is
    never produced. ``correct_when`` adds the calibration section and needs the ``policy`` extra.
    """
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise PackError(f"{directory} is not empty; pass a new directory so nothing is overwritten")
    records = list(store.iter_records(stream))
    if not records:
        raise PackError(
            f"no records in stream {stream!r}: nothing to pack"
            if stream
            else "the store holds no records: nothing to pack"
        )

    reports = verify_records(records)
    broken = [r for r in reports if not r.ok]
    if broken:
        detail = "; ".join(f"{r.stream}: {'; '.join(r.errors[:3])}" for r in broken)
        raise PackError(f"the chain does not verify, so no pack was written — {detail}")

    manifest = _describe(records)
    manifest.verified = True
    cover = coverage(store, stream=stream)

    # Resolved before a single byte is written, for the same reason the chain is verified first:
    # a half-built pack on disk is worse than none, and a cited version the registry cannot produce
    # means the questions behind those decisions are unrecoverable.
    question_payloads = _resolve_questions(questions_dir, manifest) if questions_dir else {}

    calibration = None
    if correct_when:
        from warrant.calibrate import calibrate

        calibration = calibrate(
            store, correct_when=correct_when, stream=stream, answer=answer,
            buckets=buckets, by=by, where=where,
        )

    directory.mkdir(parents=True, exist_ok=True)

    written: List[str] = []
    records_path = directory / "records.jsonl"
    with records_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    written.append("records.jsonl")

    (directory / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    written.append("manifest.json")
    (directory / "coverage.json").write_text(json.dumps(cover.to_dict(), indent=2) + "\n", encoding="utf-8")
    written.append("coverage.json")
    if calibration is not None:
        (directory / "calibration.json").write_text(
            json.dumps(calibration.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        written.append("calibration.json")

    policies_copied = _copy_policies(policy_dir, directory) if policy_dir else []
    written.extend(f"policies/{name}" for name in policies_copied)
    questions_written: List[str] = []
    if question_payloads:
        (directory / "questions").mkdir(parents=True, exist_ok=True)
        for name, payload in sorted(question_payloads.items()):
            (directory / "questions" / name).write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            questions_written.append(name)
    written.extend(f"questions/{name}" for name in questions_written)

    (directory / "README.md").write_text(
        _front_page(manifest, cover, calibration, title, policies_copied, questions_written),
        encoding="utf-8",
    )
    written.append("README.md")

    log.info(
        "evidence pack: %d record(s) from stream %s written to %s",
        manifest.records,
        stream,
        directory,
    )
    return PackResult(
        directory=directory, manifest=manifest, coverage=cover, calibration=calibration, files=written
    )


def _copy_policies(policy_dir: Path, directory: Path) -> List[str]:
    source = Path(policy_dir)
    if not source.exists():
        raise PackError(f"{source}: no policy bundle there")
    targets = sorted(
        [source] if source.is_file() else [p for p in source.iterdir() if p.suffix in (".yaml", ".yml", ".json")]
    )
    if not targets:
        raise PackError(f"{source}: no policy files to include")
    (directory / "policies").mkdir(parents=True, exist_ok=True)
    for path in targets:
        shutil.copy2(path, directory / "policies" / path.name)
    return [p.name for p in targets]


def _resolve_questions(questions_dir: Path, manifest: Manifest) -> Dict[str, Dict[str, Any]]:
    """The question sets the records actually cite, resolved and ready to write.

    Only the versions stamped on records in this period are included: a pack is evidence about
    these decisions, not a dump of every set the customer has ever written. A version a record
    cites but the registry no longer holds raises rather than being skipped — a record whose
    questions cannot be produced is a record nobody can interpret.
    """
    from warrant.questions import QuestionSetError, Registry

    try:
        registry = Registry.load(questions_dir)
    except (FileNotFoundError, QuestionSetError) as exc:
        raise PackError(str(exc)) from exc
    payloads: Dict[str, Dict[str, Any]] = {}
    for ref in sorted(manifest.question_sets):
        set_id, _, version = ref.partition("@")
        try:
            question_set = registry.get(set_id, version)
        except QuestionSetError as exc:
            raise PackError(
                f"records in this period cite {ref}, which the registry does not hold, so the "
                f"questions behind those decisions cannot be shown: {exc}"
            ) from exc
        payloads[f"{set_id}@{version}.json"] = question_set.to_dict()
    return payloads
