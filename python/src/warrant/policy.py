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
        when: amount <= 500000 && bureau_score >= 720 && foir <= 0.45
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
    _programs: List[Any] = field(default_factory=list, repr=False)


@dataclass
class PolicyTestResult:
    policy_id: str
    name: str
    passed: bool
    expected: str
    got: str
    detail: str = ""


class PolicyBundle:
    """A set of policies with a lookup from decision class to the governing policy."""

    def __init__(self, policies: Sequence[Policy]) -> None:
        self.policies: List[Policy] = list(policies)
        self._exact: Dict[str, Policy] = {}
        self._prefix: Dict[str, Policy] = {}
        for policy in self.policies:
            for cls in policy.classes:
                table = self._prefix if cls.endswith(".*") else self._exact
                key = cls[:-2] if cls.endswith(".*") else cls
                if key in table:
                    raise PolicyError(f"class {cls!r} is claimed by both {table[key].policy_id} and {policy.policy_id}")
                table[key] = policy

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

    def policy_for(self, decision_class: str) -> Optional[Policy]:
        """Exact class match first, then the longest matching ``prefix.*`` pattern."""
        if decision_class in self._exact:
            return self._exact[decision_class]
        parts = decision_class.split(".")
        for i in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in self._prefix:
                return self._prefix[prefix]
        return None


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
        _programs=programs,
    )


class CelPolicyEngine:
    """``PolicyEngine`` over a loaded bundle. Evaluation is synchronous and in-process."""

    def __init__(self, bundle: PolicyBundle) -> None:
        self.bundle = bundle
        self._celpy = _require_cel()

    def evaluate(self, decision_class: str, inputs: Mapping[str, Any]) -> Verdict:
        policy = self.bundle.policy_for(decision_class)
        if policy is None:
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
                return Verdict(clause.result, policy_id=policy.policy_id, policy_version=policy.version, clause=clause.id, reason=clause.title or f"clause {clause.id} matched")
        return Verdict(policy.default, policy_id=policy.policy_id, policy_version=policy.version, reason="no clause matched")

    @staticmethod
    def _fail(policy: Policy, detail: str) -> Verdict:
        result = _FAIL_RESULT[policy.fail_mode]
        log.warning("warrant policy %s could not evaluate (%s); fail-%s applied", policy.policy_id, detail, policy.fail_mode)
        return Verdict(result, policy_id=policy.policy_id, policy_version=policy.version, reason=f"fail-{policy.fail_mode}: {detail}", flagged=True)


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
