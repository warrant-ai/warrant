"""Replay recorded decisions against a changed target and diff the results.

A *target* names what changes: the agent version label, the model the decider should
use, the policy bundle, and the decider itself, a callable
``decider(d: Decision, inputs: dict, target: Target) -> None`` that makes one
decision with the same API as live code. In ``frozen`` mode ``d.tool()`` returns
recorded results and only model calls run; in ``live`` mode tools run again.
Replayed records never enter the ledger. The decider receives the recorded ``check()``
inputs (or whatever ``d.set_inputs()`` stored); the subject is ``d.subject``.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import traceback
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Union
from xml.sax.saxutils import escape

from warrant.client import AgentInfo, Decision, PolicyEngine, ReplaySource, Unreplayable, decode_blob
from warrant.sets import DecisionSet, SetItem

log = logging.getLogger("warrant.replay")

Decider = Callable[[Decision, Dict[str, Any], "Target"], None]
GATES = ("flipped", "new-deny", "new-escalate", "unreplayable", "errored")


@dataclass
class Target:
    name: str
    agent: AgentInfo
    model: Optional[str] = None
    policy: Optional[str] = None
    decider: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"agent": {"name": self.agent.name, "version": self.agent.version}, "model": self.model, "policy": self.policy, "decider": self.decider, "params": self.params}

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> "Target":
        agent = raw.get("agent") or {}
        if not agent.get("name") or not agent.get("version"):
            raise ValueError(f"target {name!r}: agent name and version are required")
        return cls(name=name, agent=AgentInfo(agent["name"], str(agent["version"])), model=raw.get("model"), policy=raw.get("policy"), decider=raw.get("decider"), params=raw.get("params") or {})


def load_targets(path: Union[str, Path]) -> Dict[str, Target]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {name: Target.from_dict(name, spec) for name, spec in raw.items()}


def save_targets(path: Union[str, Path], targets: Dict[str, Target]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({t.name: t.to_dict() for t in targets.values()}, indent=2) + "\n", encoding="utf-8")


def parse_agent(spec: str) -> AgentInfo:
    """``name@version`` -> AgentInfo."""
    if "@" not in spec:
        raise ValueError(f"agent must be name@version, got {spec!r}")
    name, version = spec.rsplit("@", 1)
    if not name or not version:
        raise ValueError(f"agent must be name@version, got {spec!r}")
    return AgentInfo(name, version)


def load_decider(spec: str) -> Decider:
    """Import ``package.module:function``; the working directory is on the import path."""
    if ":" not in spec:
        raise ValueError(f"decider must be module:function, got {spec!r}")
    module_name, attr = spec.split(":", 1)
    if "" not in sys.path:
        sys.path.insert(0, "")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(f"cannot import decider module {module_name!r}: {exc}") from exc
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ValueError(f"{spec!r} is not a callable")
    return fn


class FrozenSource(ReplaySource):
    """Serves each recorded tool result in call order, by evidence name."""

    def __init__(self, item: SetItem) -> None:
        self._queues: Dict[str, Deque[Any]] = defaultdict(deque)
        self._missing: Dict[str, str] = {}
        for ev in item.record.get("evidence") or []:
            if ev.get("type") != "tool_call":
                continue
            blob = item.evidence_content.get(ev["content_hash"])
            if blob is None:
                self._missing[ev["name"]] = ev["content_hash"]
                continue
            self._queues[ev["name"]].append(decode_blob(blob))

    def lookup(self, name: str) -> Any:
        queue = self._queues.get(name)
        if queue:
            return queue.popleft()
        if name in self._missing:
            raise Unreplayable(f"tool {name!r}: result was recorded by hash only; re-record with capture_evidence=True")
        raise Unreplayable(f"tool {name!r} was not called in the recorded decision")


class _ReplayClient:
    """The minimal client surface a Decision needs; collects the built record instead of emitting it."""

    def __init__(self, item: SetItem, target: Target, policy: Optional[PolicyEngine]) -> None:
        original = item.record
        self.agent = target.agent
        self.tenant = original.get("tenant", "replay")
        self.stream = original.get("stream", "replay")
        self.currency = (original.get("cost") or {}).get("currency", "USD")
        self.on_behalf_of = (original.get("actor") or {}).get("on_behalf_of")
        self.policy = policy
        self.redactor = None
        self.capture_inputs = False
        self.capture_evidence = False
        self.records: List[Dict[str, Any]] = []

    def _submit(self, record: Dict[str, Any]) -> None:
        self.records.append(record)


@dataclass
class ReplayResult:
    record_id: str
    subject: str
    decision_class: str
    status: str  # replayed | unreplayable | errored
    before_action: str
    after_action: Optional[str]
    before_mandate: str
    after_mandate: Optional[str]
    before_cost: float
    after_cost: Optional[float]
    outcome_label: Optional[str]
    detail: str = ""
    after_record: Optional[Dict[str, Any]] = None

    @property
    def flipped(self) -> bool:
        return self.status == "replayed" and self.after_action != self.before_action

    @property
    def new_deny(self) -> bool:
        return self.status == "replayed" and self.after_mandate == "deny" and self.before_mandate != "deny"

    @property
    def new_escalate(self) -> bool:
        return self.status == "replayed" and self.after_mandate == "escalate" and self.before_mandate != "escalate"

    @property
    def cost_delta(self) -> Optional[float]:
        return None if self.after_cost is None else round(self.after_cost - self.before_cost, 6)


@dataclass
class ReplayReport:
    set_name: str
    target: str
    mode: str
    results: List[ReplayResult]
    started_at: str
    finished_at: str
    fail_on: List[str] = field(default_factory=list)
    max_cost_increase: Optional[float] = None

    # -- aggregates ----------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def replayed(self) -> List[ReplayResult]:
        return [r for r in self.results if r.status == "replayed"]

    @property
    def flipped(self) -> List[ReplayResult]:
        return [r for r in self.results if r.flipped]

    @property
    def new_denies(self) -> List[ReplayResult]:
        return [r for r in self.results if r.new_deny]

    @property
    def new_escalates(self) -> List[ReplayResult]:
        return [r for r in self.results if r.new_escalate]

    @property
    def unreplayable(self) -> List[ReplayResult]:
        return [r for r in self.results if r.status == "unreplayable"]

    @property
    def errored(self) -> List[ReplayResult]:
        return [r for r in self.results if r.status == "errored"]

    @property
    def cost_before(self) -> float:
        return round(sum(r.before_cost for r in self.replayed), 6)

    @property
    def cost_after(self) -> float:
        return round(sum(r.after_cost or 0.0 for r in self.replayed), 6)

    @property
    def cost_change_pct(self) -> Optional[float]:
        if not self.replayed or self.cost_before == 0:
            return None
        return round((self.cost_after - self.cost_before) / self.cost_before * 100, 1)

    def flip_transitions(self) -> Counter:
        return Counter(f"{r.before_action} -> {r.after_action}" for r in self.flipped)

    def flips_by_outcome(self) -> Counter:
        return Counter(r.outcome_label or "(no outcome)" for r in self.flipped)

    # -- gates ---------------------------------------------------------------

    def failures(self) -> List[str]:
        """Gate violations. Errored decisions always fail the run: a crash in the decider is never a pass."""
        reasons: List[str] = []
        if self.errored:
            reasons.append(f"{len(self.errored)} decision(s) raised in the decider")
        checks = {
            "flipped": (self.flipped, "flipped decision(s) exceed threshold 0"),
            "new-deny": (self.new_denies, "new mandate denial(s)"),
            "new-escalate": (self.new_escalates, "new escalation(s)"),
            "unreplayable": (self.unreplayable, "unreplayable decision(s)"),
        }
        for gate in self.fail_on:
            if gate == "errored":
                continue
            items, label = checks[gate]
            if items:
                reasons.append(f"{len(items)} {label}")
        pct = self.cost_change_pct
        if self.max_cost_increase is not None and pct is not None and pct > self.max_cost_increase:
            reasons.append(f"cost per decision rose {pct:+.1f}%, above the {self.max_cost_increase:g}% limit")
        return reasons

    @property
    def passed(self) -> bool:
        return not self.failures()

    # -- output --------------------------------------------------------------

    def summary(self, currency: str = "") -> str:
        lines = [f"# {self.total} decisions replayed ({self.mode}) against {self.target}"]
        if self.flipped:
            trans = ", ".join(f"{n} {t}" for t, n in self.flip_transitions().most_common())
            lines.append(f"# {len(self.flipped)} flipped ({trans})")
            by_outcome = self.flips_by_outcome()
            if any(k != "(no outcome)" for k in by_outcome):
                lines.append("#   by recorded outcome: " + ", ".join(f"{n} {k}" for k, n in by_outcome.most_common()))
        else:
            lines.append("# 0 flipped")
        if self.new_denies:
            first = self.new_denies[0].after_record or {}
            m = first.get("mandate") or {}
            where = ""
            if m.get("policy_id"):
                where = f" ({m['policy_id']} clause {m['clause']})" if m.get("clause") else f" ({m['policy_id']} default)"
            lines.append(f"# {len(self.new_denies)} new deny{where}")
        if self.new_escalates:
            lines.append(f"# {len(self.new_escalates)} new escalate")
        if self.unreplayable:
            lines.append(f"# {len(self.unreplayable)} unreplayable: {self.unreplayable[0].detail}")
        if self.errored:
            lines.append(f"# {len(self.errored)} errored: {self.errored[0].detail}")
        if self.replayed:
            n = len(self.replayed)
            before, after = self.cost_before / n, self.cost_after / n
            pct = self.cost_change_pct
            change = f" ({pct:+.0f}%)" if pct is not None else ""
            lines.append(f"# cost {currency}{before:.2f} -> {currency}{after:.2f} per decision{change}")
        failures = self.failures()
        lines.append("# FAILED: " + "; ".join(failures) if failures else "# PASSED")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "set": self.set_name, "target": self.target, "mode": self.mode,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "totals": {
                "replayed": len(self.replayed), "flipped": len(self.flipped), "new_deny": len(self.new_denies),
                "new_escalate": len(self.new_escalates), "unreplayable": len(self.unreplayable), "errored": len(self.errored),
                "cost_before": self.cost_before, "cost_after": self.cost_after, "cost_change_pct": self.cost_change_pct,
            },
            "flip_transitions": dict(self.flip_transitions()),
            "flips_by_outcome": dict(self.flips_by_outcome()),
            "gates": {"fail_on": self.fail_on, "max_cost_increase": self.max_cost_increase, "failures": self.failures(), "passed": self.passed},
            "results": [{k: v for k, v in asdict(r).items() if k != "after_record"} | {"flipped": r.flipped, "new_deny": r.new_deny, "cost_delta": r.cost_delta} for r in self.results],
        }

    def to_junit(self) -> str:
        cases = []
        failures = 0
        for r in self.results:
            problems = []
            if "flipped" in self.fail_on and r.flipped:
                problems.append(f"flipped {r.before_action} -> {r.after_action}")
            if "new-deny" in self.fail_on and r.new_deny:
                problems.append("new deny")
            if "new-escalate" in self.fail_on and r.new_escalate:
                problems.append("new escalate")
            if "unreplayable" in self.fail_on and r.status == "unreplayable":
                problems.append(f"unreplayable: {r.detail}")
            if r.status == "errored":
                problems.append(f"errored: {r.detail}")
            name = escape(f"{r.decision_class} {r.subject}")
            if problems:
                failures += 1
                cases.append(f'    <testcase classname="{escape(self.set_name)}" name="{name}"><failure message="{escape("; ".join(problems))}"/></testcase>')
            else:
                cases.append(f'    <testcase classname="{escape(self.set_name)}" name="{name}"/>')
        cost_failures = [f for f in self.failures() if f.startswith("cost")]
        if cost_failures:
            failures += 1
            cases.append(f'    <testcase classname="{escape(self.set_name)}" name="cost budget"><failure message="{escape(cost_failures[0])}"/></testcase>')
        body = "\n".join(cases)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuite name="warrant test {escape(self.set_name)} against {escape(self.target)}" tests="{len(cases)}" failures="{failures}">\n'
            f"{body}\n</testsuite>\n"
        )


def replay(
    decision_set: DecisionSet,
    target: Target,
    decider: Decider,
    *,
    mode: str = "frozen",
    policy: Optional[PolicyEngine] = None,
    concurrency: int = 1,
    fail_on: Sequence[str] = (),
    max_cost_increase: Optional[float] = None,
) -> ReplayReport:
    """Replay every decision in the set and return the diff report."""
    if mode not in ("frozen", "live"):
        raise ValueError("mode must be 'frozen' or 'live'")
    for gate in fail_on:
        if gate not in GATES:
            raise ValueError(f"unknown fail-on gate {gate!r}; choose from {', '.join(GATES)}")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if policy is None and target.policy:
        from warrant.policy import CelPolicyEngine, PolicyBundle

        policy = CelPolicyEngine(PolicyBundle.load(target.policy))
    started = _now()

    def run_one(item: SetItem) -> ReplayResult:
        return _replay_item(item, target, decider, mode, policy)

    if concurrency == 1:
        results = [run_one(item) for item in decision_set]
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="warrant-replay") as pool:
            results = list(pool.map(run_one, decision_set.items))
    report = ReplayReport(decision_set.name, target.name, mode, results, started, _now(), list(fail_on), max_cost_increase)
    log.info("warrant replay %s against %s: %d replayed, %d flipped, %d unreplayable, %d errored",
             decision_set.name, target.name, len(report.replayed), len(report.flipped), len(report.unreplayable), len(report.errored))
    return report


def _replay_item(item: SetItem, target: Target, decider: Decider, mode: str, policy: Optional[PolicyEngine]) -> ReplayResult:
    original = item.record
    before = ReplayResult(
        record_id=original["record_id"],
        subject=original["decision"]["subject"],
        decision_class=original["decision"]["class"],
        status="replayed",
        before_action=original["decision"].get("action", "none"),
        after_action=None,
        before_mandate=(original.get("mandate") or {}).get("result", "unchecked"),
        after_mandate=None,
        before_cost=float((original.get("cost") or {}).get("amount", 0.0)),
        after_cost=None,
        outcome_label=item.outcome_label,
    )
    inputs = original["decision"].get("inputs")
    if inputs is None:
        before.status = "unreplayable"
        before.detail = "no recorded inputs; re-record with capture_inputs=True"
        return before
    client = _ReplayClient(item, target, policy)
    source = FrozenSource(item) if mode == "frozen" else None
    decision = Decision(
        client, before.decision_class, before.subject,
        on_behalf_of=client.on_behalf_of, alternatives=original["decision"].get("alternatives"), replay_source=source,
    )
    try:
        with decision as d:
            decider(d, dict(inputs), target)
    except Unreplayable as exc:
        before.status = "unreplayable"
        before.detail = str(exc)
        return before
    except Exception as exc:
        before.status = "errored"
        before.detail = f"{type(exc).__name__}: {exc}"
        log.debug("warrant replay decider raised for %s\n%s", before.subject, traceback.format_exc())
        return before
    record = client.records[-1] if client.records else None
    if record is None:
        before.status = "errored"
        before.detail = "decision produced no record"
        return before
    before.after_action = record["decision"].get("action", "none")
    before.after_mandate = record["mandate"]["result"]
    before.after_cost = float(record["cost"]["amount"])
    before.after_record = record
    return before


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
