"""Measure whether stated confidence matched what actually happened, on this customer's own book.

A calibrated decider that says 0.90 should be right about 90% of the time. Warrant is the only
place that holds both halves of that sentence — the confidence recorded at decision time and the
realised outcome recorded months later — so it is the only place the claim can be tested rather
than asserted.

What counts as "right" is never inferred. The caller states it as a CEL expression over the joined
record (``--correct-when "outcome.label == 'stayed_closed'"``), because guessing which outcomes
vindicate a decision is exactly the kind of quiet assumption that makes an evidence product
worthless. Decisions with no outcome yet are excluded from the curve and counted in the header:
a reliability curve over 12% of decisions is a different statement from one over 90%, and both
numbers are always shown together.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from warrant.outcomes import Coverage, iter_joined

log = logging.getLogger("warrant.calibrate")

BY_DIMENSIONS = ("class", "question_set", "route")


class CalibrationError(ValueError):
    """The calibration cannot be computed as asked. Raised before any output is produced."""


@dataclass(frozen=True)
class Point:
    """One decision reduced to the two numbers calibration needs."""

    record_id: str
    subject: str
    confidence: float
    correct: bool


@dataclass
class Bucket:
    """One confidence band of the reliability curve."""

    low: float
    high: float
    count: int = 0
    correct: int = 0
    confidence_sum: float = 0.0

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.count if self.count else 0.0

    @property
    def observed_rate(self) -> float:
        return self.correct / self.count if self.count else 0.0

    @property
    def gap(self) -> float:
        """Signed: positive means the decider was better than it claimed, negative means overconfident."""
        return self.observed_rate - self.mean_confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "count": self.count,
            "correct": self.correct,
            "mean_confidence": self.mean_confidence,
            "observed_rate": self.observed_rate,
            "gap": self.gap,
        }


@dataclass
class CalibrationReport:
    """Expected Calibration Error, a reliability curve, and the coverage the curve rests on."""

    stream: Optional[str] = None
    answer: Optional[str] = None
    correct_when: Optional[str] = None
    decisions: int = 0
    with_confidence: int = 0
    with_outcome: int = 0
    points: List[Point] = field(default_factory=list)
    buckets: List[Bucket] = field(default_factory=list)
    breakdown: Dict[str, "CalibrationReport"] = field(default_factory=dict)
    dimension: Optional[str] = None

    @property
    def usable(self) -> int:
        return len(self.points)

    @property
    def accuracy(self) -> float:
        return sum(1 for p in self.points if p.correct) / self.usable if self.usable else 0.0

    @property
    def mean_confidence(self) -> float:
        return sum(p.confidence for p in self.points) / self.usable if self.usable else 0.0

    @property
    def ece(self) -> float:
        """Expected Calibration Error: bucket sizes weighting each band's gap."""
        if not self.usable:
            return 0.0
        return sum(b.count / self.usable * abs(b.gap) for b in self.buckets if b.count)

    @property
    def mce(self) -> float:
        """Maximum Calibration Error: the worst band, which is what a validator asks about."""
        gaps = [abs(b.gap) for b in self.buckets if b.count]
        return max(gaps) if gaps else 0.0

    @property
    def brier(self) -> float:
        """Mean squared error of the stated probabilities; sensitive to sharpness as well as calibration."""
        if not self.usable:
            return 0.0
        return sum((p.confidence - (1.0 if p.correct else 0.0)) ** 2 for p in self.points) / self.usable

    def summary(self) -> str:
        lines = [
            f"calibration for stream {self.stream!r}"
            + (f", answer {self.answer!r}" if self.answer else "")
        ]
        lines.append(f"  correct when: {self.correct_when}")
        share = self.with_outcome / self.decisions if self.decisions else 0.0
        lines.append(
            f"  {self.decisions} decision(s), {self.with_confidence} with confidence, "
            f"{self.with_outcome} with an outcome ({share:.1%} attached), {self.usable} usable"
        )
        if not self.usable:
            lines.append("  nothing to measure: no decision carries both a confidence and an outcome")
            return "\n".join(lines)
        lines.append(
            f"  stated {self.mean_confidence:.3f} vs observed {self.accuracy:.3f}   "
            f"ECE {self.ece:.4f}   MCE {self.mce:.4f}   Brier {self.brier:.4f}"
        )
        lines.append("  band          n   stated  observed   gap")
        for b in self.buckets:
            if not b.count:
                continue
            lines.append(
                f"  {b.low:.2f}-{b.high:.2f} {b.count:6d}   {b.mean_confidence:.3f}     "
                f"{b.observed_rate:.3f}  {b.gap:+.3f}"
            )
        if self.breakdown:
            lines.append(f"  by {self.dimension}:")
            for key, sub in sorted(self.breakdown.items()):
                lines.append(
                    f"    {key}: n={sub.usable}  stated {sub.mean_confidence:.3f}  "
                    f"observed {sub.accuracy:.3f}  ECE {sub.ece:.4f}"
                )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stream": self.stream,
            "answer": self.answer,
            "correct_when": self.correct_when,
            "decisions": self.decisions,
            "with_confidence": self.with_confidence,
            "with_outcome": self.with_outcome,
            "outcome_attached_share": self.with_outcome / self.decisions if self.decisions else 0.0,
            "usable": self.usable,
            "mean_confidence": self.mean_confidence,
            "accuracy": self.accuracy,
            "ece": self.ece,
            "mce": self.mce,
            "brier": self.brier,
            "buckets": [b.to_dict() for b in self.buckets],
            "dimension": self.dimension,
            "breakdown": {k: v.to_dict() for k, v in sorted(self.breakdown.items())},
        }


def confidence_of(record: Dict[str, Any], answer: Optional[str]) -> Optional[float]:
    """The stated confidence for a decision, from ``decision.answers[]``.

    With ``answer`` given, only that question is read. Without it, a record carrying exactly one
    answer with a confidence is unambiguous and is used; a record carrying several is not, and
    raises rather than picking one.
    """
    answers = record.get("decision", {}).get("answers") or []
    scored = [a for a in answers if isinstance(a.get("confidence"), (int, float)) and not isinstance(a.get("confidence"), bool)]
    if answer is not None:
        for item in scored:
            if item.get("question") == answer:
                return float(item["confidence"])
        return None
    if not scored:
        return None
    if len(scored) > 1:
        names = ", ".join(sorted(str(a.get("question")) for a in scored))
        raise CalibrationError(
            f"decision {record['record_id']} carries several answers with a confidence ({names}); "
            "name one with --answer"
        )
    return float(scored[0]["confidence"])


def _bucket_key(record: Dict[str, Any], dimension: str) -> Optional[str]:
    decision = record.get("decision", {})
    if dimension == "class":
        return decision.get("class")
    if dimension == "route":
        return decision.get("route")
    if dimension == "question_set":
        qs = decision.get("question_set")
        return f"{qs['id']}@{qs['version']}" if qs else None
    if dimension.startswith("inputs."):
        value = (decision.get("inputs") or {}).get(dimension[len("inputs."):])
        return None if value is None else str(value)
    raise CalibrationError(
        f"unknown --by dimension {dimension!r}; use one of {', '.join(BY_DIMENSIONS)} or inputs.<field>"
    )


def _curve(points: Iterable[Point], buckets: int) -> List[Bucket]:
    width = 1.0 / buckets
    bands = [Bucket(low=i * width, high=(i + 1) * width) for i in range(buckets)]
    for point in points:
        index = min(buckets - 1, max(0, int(math.floor(point.confidence / width))))
        band = bands[index]
        band.count += 1
        band.confidence_sum += point.confidence
        if point.correct:
            band.correct += 1
    return bands


def calibrate(
    store: Any,
    *,
    correct_when: str,
    stream: Optional[str] = None,
    answer: Optional[str] = None,
    buckets: int = 10,
    by: Optional[str] = None,
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> CalibrationReport:
    """Compute ECE and a reliability curve over the decisions in ``store`` that have both halves.

    ``correct_when`` is a CEL expression over the joined record (decision fields plus ``outcome``),
    compiled once; it needs the ``policy`` extra. ``predicate`` overrides the compilation and exists
    for callers that already hold one.
    """
    if buckets < 2:
        raise CalibrationError("--buckets must be 2 or more")
    if not correct_when:
        raise CalibrationError("--correct-when is required: state which outcomes vindicate a decision")
    is_correct = predicate or _compile(correct_when)

    report = CalibrationReport(stream=stream, answer=answer, correct_when=correct_when, dimension=by)
    grouped: Dict[str, List[Point]] = {}
    for record in iter_joined(store, stream):
        report.decisions += 1
        has_outcome = "outcome" in record and record["outcome"].get("status") == "observed"
        if has_outcome:
            report.with_outcome += 1
        confidence = confidence_of(record, answer)
        if confidence is None:
            continue
        report.with_confidence += 1
        if not has_outcome:
            continue
        if not 0.0 <= confidence <= 1.0:
            raise CalibrationError(
                f"decision {record['record_id']} states a confidence of {confidence}, outside 0..1"
            )
        point = Point(
            record_id=record["record_id"],
            subject=record["decision"]["subject"],
            confidence=confidence,
            correct=bool(is_correct(record)),
        )
        report.points.append(point)
        if by is not None:
            key = _bucket_key(record, by)
            if key is not None:
                grouped.setdefault(key, []).append(point)

    report.buckets = _curve(report.points, buckets)
    for key, points in grouped.items():
        sub = CalibrationReport(
            stream=stream, answer=answer, correct_when=correct_when, points=list(points)
        )
        sub.decisions = len(points)
        sub.with_confidence = len(points)
        sub.with_outcome = len(points)
        sub.buckets = _curve(points, buckets)
        report.breakdown[key] = sub

    log.info(
        "calibration over stream %s: %d usable of %d decision(s), ECE %.4f",
        stream,
        report.usable,
        report.decisions,
        report.ece,
    )
    return report


def _compile(expression: str) -> Callable[[Dict[str, Any]], bool]:
    """Compile a CEL predicate over the joined record. Shares semantics with ``warrant set --where``."""
    try:
        import celpy
    except ImportError as exc:
        raise ImportError(
            '--correct-when needs the cel-python package: pip install "warrantai[policy]"'
        ) from exc
    env = celpy.Environment()
    try:
        program = env.program(env.compile(expression))
    except celpy.CELParseError as exc:
        raise CalibrationError(f"invalid --correct-when expression: {exc}") from exc

    def predicate(record: Dict[str, Any]) -> bool:
        try:
            return bool(program.evaluate(celpy.json_to_cel(record)))
        except celpy.CELEvalError:
            return False  # a record lacking a referenced field is not a match, as in set --where

    return predicate


def coverage_of(store: Any, stream: Optional[str] = None) -> Coverage:
    """Re-exported so a caller wanting only the depth metric need not reach into two modules."""
    from warrant.outcomes import coverage

    return coverage(store, stream=stream)


def gate(report: CalibrationReport, *, max_ece: Optional[float] = None, max_mce: Optional[float] = None) -> List[str]:
    """Threshold failures, for CI. Empty means the report passed every gate it was given."""
    failures: List[str] = []
    if max_ece is not None and report.ece > max_ece:
        failures.append(f"ECE {report.ece:.4f} exceeds {max_ece:.4f}")
    if max_mce is not None and report.mce > max_mce:
        failures.append(f"MCE {report.mce:.4f} exceeds {max_mce:.4f}")
    return failures
