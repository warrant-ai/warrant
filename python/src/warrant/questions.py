"""Question sets: the questions a decision was made by answering, versioned as a first-class artefact.

A decision model answers preset typed questions. Silently editing one of those questions invalidates
every historical comparison that depended on it — the reliability curve you showed a validation
committee last quarter was measured against wording that no longer exists, and nothing in the record
would say so. So question sets are versioned files in the customer's repository, the version is
stamped into every record (``decision.question_set``), and changing a set is a deployment event
reviewed like a schema migration.

A registry is a directory of YAML or JSON files, one set each::

    id: aml.alert
    version: "3.1.0"
    title: Transaction monitoring alert adjudication
    owner: fiu-ops@bank.example
    questions:
      disposition:
        primitive: choice
        instructions: Should this alert be closed as unremarkable, or escalated for investigation?
        criteria: [close, escalate]
        owner: mlro@bank.example
      structuring_pattern:
        primitive: noul
        instructions: Is there a pattern of transactions structured below the reporting threshold?

Several versions of the same ``id`` live side by side, which is what makes a change reviewable:
:func:`compare` classifies what moved between two versions and says what the version bump should
have been. ``warrant questions lint`` fails when a bump was too small for the change it carries,
which is the whole point — a question removed in a patch release is the failure this exists to stop.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

log = logging.getLogger("warrant.questions")

PRIMITIVES = ("noul", "choice", "score")
#: Primitives whose permitted answers are enumerated. Narrowing one of these is a breaking change:
#: a record carrying a value that is no longer permitted can no longer be interpreted.
ENUMERATED = ("choice", "score")
SET_ID = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
QUESTION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

#: How severe a change is, and therefore the smallest version bump that may carry it.
BREAKING, SEMANTIC, ADDITIVE, PATCH = "breaking", "semantic", "additive", "patch"
_ORDER = {PATCH: 0, ADDITIVE: 1, SEMANTIC: 2, BREAKING: 3}
_REQUIRED_BUMP = {BREAKING: "major", SEMANTIC: "minor", ADDITIVE: "minor", PATCH: "patch"}


class QuestionSetError(ValueError):
    """A question set cannot be read or is not well formed. Raised before anything uses it."""


@dataclass(frozen=True)
class Question:
    """One typed question. ``criteria`` enumerates the permitted answers for choice and score."""

    name: str
    primitive: str
    instructions: str
    criteria: Tuple[str, ...] = ()
    owner: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"primitive": self.primitive, "instructions": self.instructions}
        if self.criteria:
            out["criteria"] = list(self.criteria)
        if self.owner:
            out["owner"] = self.owner
        return out


@dataclass(frozen=True)
class QuestionSet:
    """One version of one question set, as it was published."""

    id: str
    version: str
    questions: Dict[str, Question]
    title: Optional[str] = None
    owner: Optional[str] = None
    source: Optional[Path] = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def semver(self) -> Tuple[int, int, int]:
        major, minor, patch = SEMVER.match(self.version).groups()  # validated at load
        return int(major), int(minor), int(patch)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "version": self.version}
        if self.title:
            out["title"] = self.title
        if self.owner:
            out["owner"] = self.owner
        out["questions"] = {name: q.to_dict() for name, q in sorted(self.questions.items())}
        return out


@dataclass(frozen=True)
class Change:
    """One difference between two versions of a set, and how severe it is."""

    severity: str
    question: Optional[str]
    detail: str

    def __str__(self) -> str:
        where = f"{self.question}: " if self.question else ""
        return f"[{self.severity}] {where}{self.detail}"


@dataclass
class Comparison:
    """What moved between two versions, and whether the version bump was big enough to carry it."""

    before: QuestionSet
    after: QuestionSet
    changes: List[Change] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return max((c.severity for c in self.changes), key=lambda s: _ORDER[s], default=PATCH)

    @property
    def required_bump(self) -> str:
        return _REQUIRED_BUMP[self.severity]

    @property
    def actual_bump(self) -> str:
        (a_major, a_minor, a_patch), (b_major, b_minor, b_patch) = self.before.semver, self.after.semver
        if b_major > a_major:
            return "major"
        if b_major == a_major and b_minor > a_minor:
            return "minor"
        if (b_major, b_minor) == (a_major, a_minor) and b_patch > a_patch:
            return "patch"
        return "none"

    @property
    def sufficient(self) -> bool:
        rank = {"none": 0, "patch": 1, "minor": 2, "major": 3}
        return rank[self.actual_bump] >= rank[self.required_bump]

    def summary(self) -> str:
        lines = [f"{self.before.ref} -> {self.after.ref}"]
        if not self.changes:
            lines.append("  no difference in the questions")
            return "\n".join(lines)
        for change in self.changes:
            lines.append(f"  {change}")
        verdict = "ok" if self.sufficient else "INSUFFICIENT"
        lines.append(
            f"  {self.severity} change; needs a {self.required_bump} bump, "
            f"got {self.actual_bump} ({verdict})"
        )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before.ref,
            "after": self.after.ref,
            "severity": self.severity,
            "required_bump": self.required_bump,
            "actual_bump": self.actual_bump,
            "sufficient": self.sufficient,
            "changes": [{"severity": c.severity, "question": c.question, "detail": c.detail} for c in self.changes],
        }


def compare(before: QuestionSet, after: QuestionSet) -> Comparison:
    """Classify every difference between two versions of the same set.

    The classification is what the CI gate rests on, so each rule is deliberate:

    * **breaking** — a question removed, its primitive changed, or its permitted answers narrowed.
      Records written under the old version can no longer be interpreted against the new one.
    * **semantic** — the instructions changed. Nothing breaks structurally, but the model is being
      asked a different thing, so answers are no longer comparable with the ones before it. This is
      the change most likely to be made carelessly and is the reason this module exists.
    * **additive** — a question or a permitted answer added. Old records stay readable.
    * **patch** — titles and owners; nothing that can move an answer.
    """
    if before.id != after.id:
        raise QuestionSetError(f"cannot compare different sets: {before.id!r} and {after.id!r}")
    changes: List[Change] = []

    for name in sorted(set(before.questions) - set(after.questions)):
        changes.append(Change(BREAKING, name, "question removed"))
    for name in sorted(set(after.questions) - set(before.questions)):
        changes.append(Change(ADDITIVE, name, "question added"))

    for name in sorted(set(before.questions) & set(after.questions)):
        old, new = before.questions[name], after.questions[name]
        if old.primitive != new.primitive:
            changes.append(Change(BREAKING, name, f"primitive changed from {old.primitive} to {new.primitive}"))
        removed = [c for c in old.criteria if c not in new.criteria]
        added = [c for c in new.criteria if c not in old.criteria]
        if removed:
            changes.append(Change(BREAKING, name, f"permitted answers removed: {', '.join(removed)}"))
        if added:
            changes.append(Change(ADDITIVE, name, f"permitted answers added: {', '.join(added)}"))
        if old.instructions != new.instructions:
            changes.append(
                Change(SEMANTIC, name, "instructions changed; answers are no longer comparable with earlier ones")
            )
        if old.owner != new.owner:
            changes.append(Change(PATCH, name, f"owner changed from {old.owner!r} to {new.owner!r}"))

    if before.title != after.title:
        changes.append(Change(PATCH, None, "title changed"))
    if before.owner != after.owner:
        changes.append(Change(PATCH, None, f"owner changed from {before.owner!r} to {after.owner!r}"))
    return Comparison(before=before, after=after, changes=changes)


def load_question_set(path: Path) -> QuestionSet:
    """Parse and validate one question-set file."""
    text = Path(path).read_text(encoding="utf-8")
    name = Path(path).name
    try:
        if Path(path).suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text)
    except ImportError as exc:
        raise QuestionSetError(
            f'{name}: reading YAML needs the pyyaml package: pip install "warrantai[policy]"'
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise QuestionSetError(f"{name}: cannot parse: {exc}") from exc
    except Exception as exc:  # yaml.YAMLError has many subclasses
        raise QuestionSetError(f"{name}: cannot parse: {exc}") from exc

    if not isinstance(raw, dict):
        raise QuestionSetError(f"{name}: top level must be a mapping")

    set_id = str(raw.get("id", "")).strip()
    if not SET_ID.match(set_id):
        raise QuestionSetError(f"{name}: 'id' must look like aml.alert, got {set_id!r}")
    version = str(raw.get("version", "")).strip()
    if not SEMVER.match(version):
        raise QuestionSetError(f"{name}: 'version' must be semantic, e.g. 3.1.0, got {version!r}")

    raw_questions = raw.get("questions")
    if not isinstance(raw_questions, dict) or not raw_questions:
        raise QuestionSetError(f"{name}: 'questions' must be a non-empty mapping")

    questions: Dict[str, Question] = {}
    for q_name, body in raw_questions.items():
        questions[str(q_name)] = _load_question(name, str(q_name), body)

    return QuestionSet(
        id=set_id,
        version=version,
        questions=questions,
        title=(str(raw["title"]).strip() if raw.get("title") else None),
        owner=(str(raw["owner"]).strip() if raw.get("owner") else None),
        source=Path(path),
    )


def _load_question(file_name: str, name: str, body: Any) -> Question:
    where = f"{file_name}: question {name!r}"
    if not QUESTION_NAME.match(name):
        raise QuestionSetError(f"{where}: name must be lower_snake_case starting with a letter")
    if not isinstance(body, dict):
        raise QuestionSetError(f"{where}: must be a mapping")
    primitive = str(body.get("primitive", "")).strip().lower()
    if primitive not in PRIMITIVES:
        raise QuestionSetError(f"{where}: 'primitive' must be one of {', '.join(PRIMITIVES)}, got {primitive!r}")
    instructions = str(body.get("instructions", "")).strip()
    if not instructions:
        raise QuestionSetError(f"{where}: 'instructions' must be a non-empty string")

    raw_criteria = body.get("criteria")
    criteria: Tuple[str, ...] = ()
    if primitive in ENUMERATED:
        if isinstance(raw_criteria, dict):
            raw_criteria = list(raw_criteria)
        if not isinstance(raw_criteria, list) or len(raw_criteria) < 2:
            raise QuestionSetError(
                f"{where}: a {primitive} needs 'criteria' listing at least two permitted answers"
            )
        values = [str(c).strip() for c in raw_criteria]
        if any(not v for v in values):
            raise QuestionSetError(f"{where}: 'criteria' entries must be non-empty")
        if len(set(values)) != len(values):
            raise QuestionSetError(f"{where}: 'criteria' has duplicate entries")
        criteria = tuple(values)
    elif raw_criteria is not None:
        raise QuestionSetError(f"{where}: a noul takes no 'criteria'; it is always yes or no")

    owner = body.get("owner")
    return Question(
        name=name,
        primitive=primitive,
        instructions=instructions,
        criteria=criteria,
        owner=(str(owner).strip() if owner else None),
    )


@dataclass
class Registry:
    """Every version of every question set in a directory, indexed by id and version."""

    sets: Dict[str, Dict[str, QuestionSet]] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Registry":
        """Load every ``*.yaml``, ``*.yml`` and ``*.json`` file in a directory, or one file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"question set registry not found: {path}")
        files = (
            [path]
            if path.is_file()
            else sorted(p for p in path.iterdir() if p.suffix.lower() in (".yaml", ".yml", ".json"))
        )
        if not files:
            raise QuestionSetError(f"no question set files (*.yaml, *.yml, *.json) in {path}")
        registry = cls(path=path)
        for file in files:
            question_set = load_question_set(file)
            versions = registry.sets.setdefault(question_set.id, {})
            if question_set.version in versions:
                first = versions[question_set.version].source
                raise QuestionSetError(
                    f"{file.name}: {question_set.ref} is already defined in "
                    f"{first.name if first else 'another file'}"
                )
            versions[question_set.version] = question_set
        log.info(
            "question set registry loaded: %d set(s), %d version(s) from %s",
            len(registry.sets),
            sum(len(v) for v in registry.sets.values()),
            path,
        )
        return registry

    def get(self, set_id: str, version: str) -> QuestionSet:
        """One pinned version. Raises rather than falling back to anything else."""
        versions = self.sets.get(set_id)
        if versions is None:
            known = ", ".join(sorted(self.sets)) or "none"
            raise QuestionSetError(f"unknown question set {set_id!r}; the registry holds: {known}")
        if version not in versions:
            known = ", ".join(sorted(versions, key=_sort_key))
            raise QuestionSetError(
                f"{set_id}@{version} is not in the registry; it holds: {known}"
            )
        return versions[version]

    def latest(self, set_id: str) -> QuestionSet:
        versions = self.sets.get(set_id)
        if not versions:
            raise QuestionSetError(f"unknown question set {set_id!r}")
        return versions[max(versions, key=_sort_key)]

    def history(self, set_id: str) -> List[QuestionSet]:
        """Every version of one set, oldest first."""
        versions = self.sets.get(set_id)
        if not versions:
            raise QuestionSetError(f"unknown question set {set_id!r}")
        return [versions[v] for v in sorted(versions, key=_sort_key)]

    def refs(self) -> List[str]:
        return [s.ref for set_id in sorted(self.sets) for s in self.history(set_id)]


def _sort_key(version: str) -> Tuple[int, int, int]:
    major, minor, patch = SEMVER.match(version).groups()
    return int(major), int(minor), int(patch)


@dataclass
class LintReport:
    """What ``warrant questions lint`` found. Empty ``problems`` means the registry is publishable."""

    registry: Registry
    comparisons: List[Comparison] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        total = sum(len(v) for v in self.registry.sets.values())
        lines = [f"{len(self.registry.sets)} question set(s), {total} version(s)"]
        for set_id in sorted(self.registry.sets):
            versions = ", ".join(s.version for s in self.registry.history(set_id))
            lines.append(f"  {set_id}: {versions}")
        for comparison in self.comparisons:
            if comparison.changes:
                lines.append("  " + comparison.summary().replace("\n", "\n  "))
        if self.problems:
            lines.append(f"  {len(self.problems)} problem(s):")
            lines.extend(f"    {p}" for p in self.problems)
        else:
            lines.append("  no problems")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sets": {k: [s.version for s in self.registry.history(k)] for k in sorted(self.registry.sets)},
            "comparisons": [c.to_dict() for c in self.comparisons],
            "problems": list(self.problems),
            "ok": self.ok,
        }


def lint(registry: Registry) -> LintReport:
    """Check every consecutive pair of versions and report bumps too small for their change.

    A question removed in a patch release is the failure this exists to stop: records written
    against the old version silently stop being comparable, and the version string gives no hint.
    """
    report = LintReport(registry=registry)
    for set_id in sorted(registry.sets):
        history = registry.history(set_id)
        for before, after in zip(history, history[1:]):
            comparison = compare(before, after)
            report.comparisons.append(comparison)
            if not comparison.sufficient:
                worst = [str(c) for c in comparison.changes if c.severity == comparison.severity]
                report.problems.append(
                    f"{before.ref} -> {after.ref} is a {comparison.severity} change carried by a "
                    f"{comparison.actual_bump} bump; needs {comparison.required_bump}. "
                    + "; ".join(worst)
                )
    return report


def check_answers(question_set: QuestionSet, answers: Mapping[str, Any]) -> List[str]:
    """Problems with a set of answers against the questions that were asked, as plain sentences.

    Used by the adapter after a model replies, so an answer outside the permitted values is caught
    where it happened rather than in a reliability curve three months later.
    """
    problems: List[str] = []
    for name in sorted(set(question_set.questions) - set(answers)):
        problems.append(f"{name}: not answered")
    for name in sorted(set(answers) - set(question_set.questions)):
        problems.append(f"{name}: answered but not in {question_set.ref}")
    for name in sorted(set(answers) & set(question_set.questions)):
        question = question_set.questions[name]
        value = getattr(answers[name], "value", answers[name])
        if question.primitive == "noul" and not isinstance(value, bool):
            problems.append(f"{name}: a noul must answer true or false, got {value!r}")
        elif question.primitive == "choice" and str(value) not in question.criteria:
            problems.append(
                f"{name}: {value!r} is not one of the permitted answers ({', '.join(question.criteria)})"
            )
    return problems
