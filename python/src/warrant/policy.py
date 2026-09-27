"""Policy bundles: versioned CEL policies mapped to decision classes, evaluated locally.

A bundle is a directory of YAML or JSON files, one policy each::

    policy_id: CR-07
    version: "2026.3"
    title: Retail credit auto-approval
    classes: [credit.approve]
    fail_mode: closed          # what check() returns if a clause cannot be evaluated
    default: deny              # result when no clause matches
    clauses:
      - id: "4.2"
        title: Auto-approve within limit
        when: amount <= 500000 && bureau_score >= 720 && double(foir) <= 0.45
        result: allow
    tests:
      - name: within limit
        inputs: {amount: 450000, bureau_score: 748, foir: 0.38}
        expect: allow
        clause: "4.2"

Clauses are evaluated in order; the first whose ``when`` is true decides. Requires
the ``policy`` extra: ``pip install "warrantai[policy]"``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from warrant.client import MANDATE_RESULTS, Verdict

log = logging.getLogger("warrant.policy")

FAIL_MODES = ("closed", "open", "escalate")
CLAUSE_RESULTS = ("allow", "deny", "escalate")
_CLASS_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*(\.\*)?$")
_FAIL_RESULT = {"closed": "deny", "open": "allow", "escalate": "escalate"}


# An input compared directly with a decimal literal. A whole-number input (0, 1, 40) is an
# int, and CEL engines disagree on int-versus-double comparison; double(x) is portable.
_BARE_DECIMAL_COMPARISON = re.compile(
    r"(?<![\w.)])([a-z_][\w.]*)\s*(?:<=|>=|==|!=|<|>)\s*\d+\.\d+|\d+\.\d+\s*(?:<=|>=|==|!=|<|>)\s*([a-z_][\w.]*)(?![\w.(])", re.IGNORECASE
)
# One input divided by another. Whole numbers divide as integers (30000 / 50000 is 0).
_BARE_DIVISION = re.compile(r"(?<![\w.)])([a-z_][\w.]*)\s*/\s*([a-z_][\w.]*)(?![\w.(])", re.IGNORECASE)
_ENDS_WITH_OPERATOR = re.compile(r"[-+*/%]\s*$")


def lint_clause(when: str) -> List[str]:
    """Warn-worthy patterns in a clause. Returned, not raised: the policy still loads."""
    warnings: List[str] = []
    for match in _BARE_DECIMAL_COMPARISON.finditer(when):
        # `a / b <= 0.45` compares the quotient, not b; the division warning covers it.
        if _ENDS_WITH_OPERATOR.search(when[: match.start()]):
            continue
        name = match.group(1) or match.group(2)
        warnings.append(f"compares {name} with a decimal literal; a whole-number input cannot be evaluated by every engine, write double({name})")
    division = _BARE_DIVISION.search(when)
    if division:
        a, b = division.group(1), division.group(2)
        warnings.append(f"divides {a} by {b}; whole numbers divide as integers, write double({a}) / double({b})")
    return warnings


class PolicyError(ValueError):
    """A policy file is malformed, a CEL expression does not parse, or the bundle is inconsistent."""


def _require_cel():
    try:
        import celpy  # noqa: F401
    except ImportError as exc:
        raise ImportError('policy bundles need the cel-python package: pip install "warrantai[policy]"') from exc
    return celpy


@dataclass(frozen=True)
class Clause:
    id: str
    when: str
    result: str
    title: Optional[str] = None


OBLIGATION_KINDS = ("verifiable", "advisory")
OBLIGATION_TYPES = ("model_call", "tool_call", "document", "web", "other", "record", "mandate", "attestation", "human_review")
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any, where: str) -> int:
    """``30d``, ``12h``, ``90m``, ``45s`` or a whole number of seconds."""
    if isinstance(value, bool):
        raise PolicyError(f"{where}: not a duration: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise PolicyError(f"{where}: a duration cannot be negative")
        return value
    match = _DURATION.match(str(value))
    if not match:
        raise PolicyError(f"{where}: not a duration (use e.g. 30d, 12h, 90m or seconds): {value!r}")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


@dataclass(frozen=True)
class ObligationSpec:
    """What has to be true before a decision of this class is warranted (ADR 1.4).

    ``when`` is an optional CEL condition over the same inputs as the clauses, for obligations that
    apply only sometimes: an officer's sign-off above an approval limit.
    """

    id: str
    requires: str
    kind: str = "verifiable"
    providers: Tuple[str, ...] = ()
    max_age_seconds: Optional[int] = None
    name: Optional[str] = None
    clause: Optional[str] = None
    title: Optional[str] = None
    when: Optional[str] = None

    def to_record(self, policy: "Policy") -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "requires": self.requires, "kind": self.kind,
                               "policy_id": policy.policy_id, "policy_version": policy.version}
        if self.providers:
            out["providers"] = list(self.providers)
        for key in ("max_age_seconds", "name", "clause", "title"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class PolicyTest:
    name: str
    inputs: Dict[str, Any]
    expect: str
    clause: Optional[str] = None


@dataclass
class Policy:
    policy_id: str
    version: str
    classes: List[str]
    clauses: List[Clause]
    fail_mode: str = "closed"
    default: str = "deny"
    title: Optional[str] = None
    tests: List[PolicyTest] = field(default_factory=list)
    source: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    obligations: List[ObligationSpec] = field(default_factory=list)
    enforce: bool = False
    retention: Optional[Dict[str, Any]] = None
    _programs: List[Any] = field(default_factory=list, repr=False)
    _obligation_programs: List[Any] = field(default_factory=list, repr=False)

    def covers(self, at: Optional[str]) -> bool:
        """Was this version in force at ``at``? An undated policy is in force always."""
        if at is None or (self.effective_from is None and self.effective_to is None):
            return self.effective_from is None or self.effective_from <= (at or "")
        if self.effective_from is not None and at < self.effective_from:
            return False
        if self.effective_to is not None and at >= self.effective_to:
            return False
        return True

    @property
    def window(self) -> str:
        if self.effective_from is None and self.effective_to is None:
            return "always"
        return f"{self.effective_from or 'the beginning'} to {self.effective_to or 'further notice'}"


@dataclass
class PolicyTestResult:
    policy_id: str
    name: str
    passed: bool
    expected: str
    got: str
    detail: str = ""




def _as_date(file_name: str, value: Any, field_name: str) -> Optional[str]:
    """A date or timestamp, normalised so string comparison against a record timestamp is correct.

    A bare ``2026-10-01`` becomes ``2026-10-01T00:00:00Z``: policies are written by compliance
    teams in dates, and records carry RFC 3339 timestamps, so the two have to be made comparable
    exactly once, here, rather than at every comparison.
    """
    if value is None:
        return None
    from datetime import date, datetime

    if isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, date):
        text = f"{value.isoformat()}T00:00:00Z"
    else:
        text = str(value).strip()
        if not text:
            return None
        if len(text) == 10:
            text = f"{text}T00:00:00Z"
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError(f"{file_name}: {field_name} is not a date or timestamp: {value!r}") from exc
    return text if text.endswith("Z") else text

def _overlaps(a: Policy, b: Policy) -> bool:
    """Do two versions of a policy both govern at some moment? Undated means always."""
    if a.effective_from is None and a.effective_to is None:
        return True
    if b.effective_from is None and b.effective_to is None:
        return True
    a_from, a_to = a.effective_from or "", a.effective_to or "9999"
    b_from, b_to = b.effective_from or "", b.effective_to or "9999"
    return a_from < b_to and b_from < a_to


def _in_force(candidates: Optional[Sequence[Policy]], at: Optional[str]) -> Optional[Policy]:
    if not candidates:
        return None
    if at is None:
        # No moment given: the version in force now, which is the newest that has started.
        now = _now_iso()
        for policy in candidates:
            if policy.covers(now):
                return policy
        return None
    for policy in candidates:
        if policy.covers(at):
            return policy
    return None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class PolicyBundle:
    """A set of policies with a lookup from decision class to the governing policy."""

    def __init__(self, policies: Sequence[Policy]) -> None:
        self.policies: List[Policy] = list(policies)
        self._exact: Dict[str, List[Policy]] = {}
        self._prefix: Dict[str, List[Policy]] = {}
        for policy in self.policies:
            for cls in policy.classes:
                table = self._prefix if cls.endswith(".*") else self._exact
                key = cls[:-2] if cls.endswith(".*") else cls
                for existing in table.get(key, ()):
                    if _overlaps(existing, policy):
                        raise PolicyError(
                            f"class {cls!r} is claimed by both {existing.policy_id} "
                            f"({existing.window}) and {policy.policy_id} ({policy.window}); "
                            "give each an effective_from so only one governs at a time"
                        )
                table.setdefault(key, []).append(policy)
        # Newest first, so selection is the first version whose window covers the moment.
        for table in (self._exact, self._prefix):
            for key in table:
                table[key].sort(key=lambda p: (p.effective_from or ""), reverse=True)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "PolicyBundle":
        """Load every ``*.yaml``, ``*.yml`` and ``*.json`` file in a directory, or one file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"policy bundle not found: {path}")
        files = [path] if path.is_file() else sorted(p for p in path.iterdir() if p.suffix.lower() in (".yaml", ".yml", ".json"))
        if not files:
            raise PolicyError(f"no policy files (*.yaml, *.yml, *.json) in {path}")
        policies = [load_policy(f) for f in files]
        bundle = cls(policies)
        log.info("warrant policy bundle loaded: %d policy file(s) from %s", len(policies), path)
        return bundle

    def policy_for(self, decision_class: str, at: Optional[str] = None) -> Optional[Policy]:
        """The policy governing this class at ``at``: exact match first, then the longest prefix.

        ``at`` is the decision's own timestamp, not the reader's clock. That is what lets a record
        written in October be replayed in March and still be judged against the policy that was
        actually in force when it was made — which is the whole reason versions carry dates.
        """
        chosen = _in_force(self._exact.get(decision_class), at)
        if chosen is not None:
            return chosen
        parts = decision_class.split(".")
        for i in range(len(parts) - 1, 0, -1):
            chosen = _in_force(self._prefix.get(".".join(parts[:i])), at)
            if chosen is not None:
                return chosen
        return None

    def versions_for(self, decision_class: str) -> List[Policy]:
        """Every dated version claiming this class, newest first. For explaining a selection."""
        return list(self._exact.get(decision_class) or [])


def load_policy(path: Path) -> Policy:
    celpy = _require_cel()
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyError(f"{path.name}: cannot parse: {exc}") from exc
    except Exception as exc:  # yaml.YAMLError has many subclasses
        raise PolicyError(f"{path.name}: cannot parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path.name}: top level must be a mapping")

    def need_str(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PolicyError(f"{path.name}: {key!r} must be a non-empty string")
        return value.strip()

    policy_id = need_str("policy_id")
    version = str(raw.get("version", "")).strip()
    if not version:
        raise PolicyError(f"{path.name}: 'version' must be a non-empty string")
    classes = raw.get("classes")
    if not isinstance(classes, list) or not classes or not all(isinstance(c, str) for c in classes):
        raise PolicyError(f"{path.name}: 'classes' must be a non-empty list of decision classes")
    for c in classes:
        if not _CLASS_PATTERN.match(c):
            raise PolicyError(f"{path.name}: invalid class pattern {c!r} (expected e.g. credit.approve or credit.*)")
    effective_from = _as_date(path.name, raw.get("effective_from"), "effective_from")
    effective_to = _as_date(path.name, raw.get("effective_to"), "effective_to")
    if effective_from and effective_to and effective_to <= effective_from:
        raise PolicyError(f"{path.name}: effective_to must be after effective_from")
    fail_mode = str(raw.get("fail_mode", "closed"))
    if fail_mode not in FAIL_MODES:
        raise PolicyError(f"{path.name}: fail_mode must be one of {FAIL_MODES}, got {fail_mode!r}")
    default = str(raw.get("default", "deny"))
    if default not in CLAUSE_RESULTS:
        raise PolicyError(f"{path.name}: default must be one of {CLAUSE_RESULTS}, got {default!r}")

    raw_clauses = raw.get("clauses")
    if not isinstance(raw_clauses, list) or not raw_clauses:
        raise PolicyError(f"{path.name}: 'clauses' must be a non-empty list")
    env = celpy.Environment()
    clauses: List[Clause] = []
    programs: List[Any] = []
    seen_ids = set()
    for i, rc in enumerate(raw_clauses):
        if not isinstance(rc, dict):
            raise PolicyError(f"{path.name}: clause #{i + 1} must be a mapping")
        cid = str(rc.get("id", "")).strip()
        if not cid:
            raise PolicyError(f"{path.name}: clause #{i + 1} has no id")
        if cid in seen_ids:
            raise PolicyError(f"{path.name}: duplicate clause id {cid!r}")
        seen_ids.add(cid)
        when = rc.get("when")
        if not isinstance(when, str) or not when.strip():
            raise PolicyError(f"{path.name}: clause {cid}: 'when' must be a CEL expression string")
        result = str(rc.get("result", ""))
        if result not in CLAUSE_RESULTS:
            raise PolicyError(f"{path.name}: clause {cid}: result must be one of {CLAUSE_RESULTS}, got {result!r}")
        try:
            programs.append(env.program(env.compile(when)))
        except celpy.CELParseError as exc:
            raise PolicyError(f"{path.name}: clause {cid}: CEL parse error: {exc}") from exc
        for warning in lint_clause(when):
            log.warning("warrant policy %s clause %s %s", policy_id, cid, warning)
        title = rc.get("title")
        clauses.append(Clause(id=cid, when=when.strip(), result=result, title=str(title) if title else None))

    tests: List[PolicyTest] = []
    for i, rt in enumerate(raw.get("tests") or []):
        if not isinstance(rt, dict) or not isinstance(rt.get("inputs"), dict):
            raise PolicyError(f"{path.name}: test #{i + 1} must be a mapping with an 'inputs' mapping")
        expect = str(rt.get("expect", ""))
        if expect not in MANDATE_RESULTS:
            raise PolicyError(f"{path.name}: test #{i + 1}: expect must be one of {MANDATE_RESULTS}, got {expect!r}")
        clause = rt.get("clause")
        tests.append(PolicyTest(name=str(rt.get("name") or f"test #{i + 1}"), inputs=dict(rt["inputs"]), expect=expect, clause=str(clause) if clause is not None else None))

    obligations, obligation_programs = _load_obligations(path.name, raw.get("obligations"), env, celpy, {c.id for c in clauses})
    enforce = raw.get("enforce", False)
    if not isinstance(enforce, bool):
        raise PolicyError(f"{path.name}: 'enforce' must be true or false")
    retention = _load_retention(path.name, raw.get("retention"))

    title = raw.get("title")
    return Policy(
        policy_id=policy_id,
        version=version,
        classes=[c.strip() for c in classes],
        clauses=clauses,
        fail_mode=fail_mode,
        default=default,
        title=str(title) if title else None,
        tests=tests,
        source=path.name,
        effective_from=effective_from,
        effective_to=effective_to,
        obligations=obligations,
        enforce=enforce,
        retention=retention,
        _programs=programs,
        _obligation_programs=obligation_programs,
    )


def _load_obligations(file_name: str, raw: Any, env: Any, celpy: Any, clause_ids: set) -> Tuple[List[ObligationSpec], List[Any]]:
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        raise PolicyError(f"{file_name}: 'obligations' must be a list")
    specs: List[ObligationSpec] = []
    programs: List[Any] = []
    seen = set()
    for i, ro in enumerate(raw):
        where = f"{file_name}: obligation #{i + 1}"
        if not isinstance(ro, dict):
            raise PolicyError(f"{where} must be a mapping")
        oid = str(ro.get("id", "")).strip()
        if not oid:
            raise PolicyError(f"{where} has no id")
        if oid in seen:
            raise PolicyError(f"{file_name}: duplicate obligation id {oid!r}")
        seen.add(oid)
        where = f"{file_name}: obligation {oid}"
        requires = str(ro.get("requires", ""))
        if requires not in OBLIGATION_TYPES:
            raise PolicyError(f"{where}: requires must be one of {OBLIGATION_TYPES}, got {requires!r}")
        kind = str(ro.get("kind", "verifiable"))
        if kind not in OBLIGATION_KINDS:
            raise PolicyError(f"{where}: kind must be verifiable or advisory, got {kind!r}")
        providers = ro.get("providers") or []
        if not isinstance(providers, list) or not all(isinstance(p, str) and p for p in providers):
            raise PolicyError(f"{where}: providers must be a list of provider names")
        if "self" in providers:
            raise PolicyError(f"{where}: 'self' cannot be a qualified provider; the acting agent never supplies evidence for its own obligation")
        max_age = parse_duration(ro["max_age"], f"{where}: max_age") if ro.get("max_age") is not None else None
        clause = ro.get("clause")
        if clause is not None and str(clause) not in clause_ids:
            raise PolicyError(f"{where}: clause {clause!r} is not a clause of this policy")
        when = ro.get("when")
        program = None
        if when is not None:
            if not isinstance(when, str) or not when.strip():
                raise PolicyError(f"{where}: 'when' must be a CEL expression string")
            try:
                program = env.program(env.compile(when))
            except celpy.CELParseError as exc:
                raise PolicyError(f"{where}: CEL parse error: {exc}") from exc
        specs.append(ObligationSpec(
            id=oid, requires=requires, kind=kind, providers=tuple(providers), max_age_seconds=max_age,
            name=str(ro["name"]) if ro.get("name") else None, clause=str(clause) if clause is not None else None,
            title=str(ro["title"]) if ro.get("title") else None, when=when.strip() if when else None,
        ))
        programs.append(program)
    return specs, programs


def _load_retention(file_name: str, raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("class"), str) or not raw["class"]:
        raise PolicyError(f"{file_name}: retention must be a mapping with a 'class' name")
    out: Dict[str, Any] = {"class": raw["class"]}
    if raw.get("period") is not None:
        out["seconds"] = parse_duration(raw["period"], f"{file_name}: retention period")
    return out


class CelPolicyEngine:
    """``PolicyEngine`` over a loaded bundle. Evaluation is synchronous and in-process."""

    def __init__(self, bundle: PolicyBundle) -> None:
        self.bundle = bundle
        self._celpy = _require_cel()

    def evaluate(self, decision_class: str, inputs: Mapping[str, Any], at: Optional[str] = None) -> Verdict:
        """Evaluate against the policy in force at ``at`` — the decision's own timestamp.

        Passing the record's timestamp rather than the reader's clock is what makes a replay of a
        historical decision honest after a policy change: it is judged by the rules that applied
        when it was made, not by today's.
        """
        policy = self.bundle.policy_for(decision_class, at)
        if policy is None:
            known = self.bundle.versions_for(decision_class)
            if known:
                windows = "; ".join(f"{p.policy_id}@{p.version} ({p.window})" for p in known)
                return Verdict(
                    "unchecked",
                    reason=f"no version of the policy for {decision_class} was in force at {at}: {windows}",
                )
            return Verdict("unchecked", reason=f"no policy governs class {decision_class}")
        try:
            activation = self._celpy.json_to_cel(_jsonable(inputs))
        except (TypeError, ValueError) as exc:
            return self._fail(policy, f"inputs are not JSON-serialisable: {exc}")
        for clause, program in zip(policy.clauses, policy._programs):
            try:
                matched = program.evaluate(activation)
            except self._celpy.CELEvalError as exc:
                return self._fail(policy, f"clause {clause.id}: {_short(exc)}")
            if not isinstance(matched, bool) and type(matched).__name__ != "BoolType":
                return self._fail(policy, f"clause {clause.id}: expression returned {type(matched).__name__}, not bool")
            if matched:
                return self._with_adr(policy, activation, Verdict(clause.result, policy_id=policy.policy_id, policy_version=policy.version, clause=clause.id, reason=clause.title or f"clause {clause.id} matched"))
        return self._with_adr(policy, activation, Verdict(policy.default, policy_id=policy.policy_id, policy_version=policy.version, reason="no clause matched"))

    def _with_adr(self, policy: Policy, activation: Any, verdict: Verdict) -> Verdict:
        """Attach the obligations that apply, the enforcement mode and the retention class.

        An obligation whose ``when`` cannot be evaluated applies: failing towards more evidence is
        the only safe direction for a rule that decides what has to be proven.
        """
        if not policy.obligations and not policy.enforce and policy.retention is None:
            return verdict
        applicable = []
        for spec, program in zip(policy.obligations, policy._obligation_programs):
            if program is not None and activation is not None:
                try:
                    applies = program.evaluate(activation)
                except self._celpy.CELEvalError as exc:
                    log.warning("warrant policy %s obligation %s condition could not evaluate (%s); it applies", policy.policy_id, spec.id, _short(exc))
                    applies = True
                if not bool(applies):
                    continue
            applicable.append(spec.to_record(policy))
        return dataclasses.replace(verdict, obligations=tuple(applicable), enforce=policy.enforce, retention=policy.retention)

    def _fail(self, policy: Policy, detail: str) -> Verdict:
        result = _FAIL_RESULT[policy.fail_mode]
        log.warning("warrant policy %s could not evaluate (%s); fail-%s applied", policy.policy_id, detail, policy.fail_mode)
        return self._with_adr(policy, None, Verdict(result, policy_id=policy.policy_id, policy_version=policy.version, reason=f"fail-{policy.fail_mode}: {detail}", flagged=True))


def run_policy_tests(bundle: PolicyBundle) -> List[PolicyTestResult]:
    """Run every policy's embedded tests. Each test evaluates against the policy's first class."""
    engine = CelPolicyEngine(bundle)
    results: List[PolicyTestResult] = []
    for policy in bundle.policies:
        target = policy.classes[0]
        if target.endswith(".*"):
            target = target[:-2] + ".test"
        for test in policy.tests:
            verdict = engine.evaluate(target, test.inputs)
            passed = verdict.result == test.expect and (test.clause is None or verdict.clause == test.clause)
            detail = ""
            if not passed:
                want = test.expect + (f" via clause {test.clause}" if test.clause else "")
                got = verdict.result + (f" via clause {verdict.clause}" if verdict.clause else "")
                detail = f"expected {want}, got {got} ({verdict.reason})"
            results.append(PolicyTestResult(policy.policy_id, test.name, passed, test.expect, verdict.result, detail))
    return results


def _jsonable(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a mapping")
    return json.loads(json.dumps(dict(inputs)))


def _short(exc: Exception) -> str:
    text = str(exc)
    return text.split(" (in activation", 1)[0][:160]
