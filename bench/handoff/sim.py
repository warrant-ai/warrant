"""The simulated pipeline: a co-operative bank's loan appraisal run by up to five agents.

Every agent is deterministic given the case seed. Faults are injected at a known (agent, step), so
the ground truth for blame is known exactly. Each run produces three views of the same events:

* ``trace_text``: raw, free-text traces, what an LLM judge would read;
* ``logs``: structured JSON log lines, what a log pipeline holds;
* ``records``: signed Agent Decision Records written through the real Warrant SDK.

What is simulated and what is a parameter, stated plainly:

* Fault kinds, where they can occur and how often are parameters (``FAULTS``, ``fault_rate``).
* The Kannada-English code-mixed form changes the extraction agent's share of faults only by the
  ``mixed_extraction_weight`` parameter. The simulation does not discover a language effect; it
  reproduces the one it is given. Real agents are needed to measure one.
* Agents act on their inputs whether or not their record is warranted (observe mode), so all three
  views describe the same pipeline and differ only in what an attributer can see.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from warrant import AgentInfo
from warrant.client import Decision

AGENTS_BY_DEPTH: Dict[int, List[str]] = {
    3: ["intake", "extraction", "credit"],
    4: ["intake", "extraction", "rule_check", "credit"],
    5: ["intake", "extraction", "rule_check", "credit", "maker_checker"],
}
STEPS: Dict[str, List[str]] = {
    "intake": ["receive", "kyc"],
    "extraction": ["parse_documents", "extract_turnover"],
    "rule_check": ["fetch_bureau", "apply_rbi_rules"],
    "credit": ["score", "decide"],
    "maker_checker": ["review", "approve"],
}
#: (agent, step) -> (tool, qualified providers, max age in days). Steps absent here call no tool.
TOOLS: Dict[Tuple[str, str], Tuple[str, Tuple[str, ...], int]] = {
    ("intake", "kyc"): ("ckyc_lookup", ("ckyc",), 365),
    ("extraction", "parse_documents"): ("document_fetch", ("bank-docs",), 90),
    ("rule_check", "fetch_bureau"): ("bureau_pull", ("cibil", "experian"), 30),
    ("rule_check", "apply_rbi_rules"): ("rbi_rules_engine", ("rules-engine",), 1),
    ("credit", "score"): ("bureau_pull", ("cibil", "experian"), 30),  # only when there is no rule_check agent
    ("maker_checker", "review"): ("gstn_turnover", ("gstn",), 30),
}
FAULT_KINDS = ("false_completion", "wrong_value", "bad_source", "skipped_check")


def faults_for(agent: str, step: str, depth: int) -> List[str]:
    """Which fault kinds can occur at a step. A parameter of the simulation, not a finding."""
    table = {
        ("intake", "kyc"): ["false_completion", "bad_source"],
        ("extraction", "parse_documents"): ["bad_source"],
        ("extraction", "extract_turnover"): ["wrong_value"],
        ("rule_check", "fetch_bureau"): ["bad_source", "false_completion"],
        ("rule_check", "apply_rbi_rules"): ["skipped_check"],
        ("credit", "score"): ["bad_source", "false_completion"] if depth == 3 else ["wrong_value"],
        ("credit", "decide"): ["wrong_value"],
        ("maker_checker", "review"): ["skipped_check", "bad_source"],
    }
    return table.get((agent, step), [])


PLACES = ["Mysuru", "Hubballi", "Mandya", "Tumakuru", "Shivamogga", "Udupi"]
TRADES = [("textile shop", "batte angadi", "ಬಟ್ಟೆ ಅಂಗಡಿ"), ("provision store", "dinasi angadi", "ದಿನಸಿ ಅಂಗಡಿ"),
          ("dairy unit", "halina ghataka", "ಹಾಲಿನ ಘಟಕ"), ("auto repair works", "auto repair works", "ಆಟೋ ರಿಪೇರಿ")]


def narrative(form: str, place: str, trade: Tuple[str, str, str], turnover: float) -> str:
    if form == "english":
        return f"The applicant runs a {trade[0]} in {place}. Annual turnover is about {turnover:.1f} crore as per GST returns."
    return (f"ಅರ್ಜಿದಾರರು {place}-ನಲ್ಲಿ {trade[2]} ({trade[1]}) nadesuttare. Varshika turnover sumaru "
            f"{turnover:.1f} crore ಎಂದು GST returns-alli ide.")


@dataclass
class Fault:
    agent: str
    step: str
    kind: str


@dataclass
class Case:
    case_id: str
    depth: int
    form: str
    cross_org: bool
    turnover: float
    bureau: int
    kyc_ok: bool
    amount_lakh: int
    text: str
    fault: Optional[Fault]
    seed: int

    @property
    def agents(self) -> List[str]:
        return AGENTS_BY_DEPTH[self.depth]

    def partner_agents(self) -> List[str]:
        """Across organisations, a partner runs intake and extraction and shares only what it sends."""
        return ["intake", "extraction"] if self.cross_org else []

    def correct_decision(self) -> str:
        return decide_rule(self.kyc_ok, self.bureau, self.turnover, self.amount_lakh)


def decide_rule(kyc_ok: bool, bureau: int, turnover: float, amount_lakh: float) -> str:
    rules_pass = kyc_ok and bureau >= 650
    return "approve" if rules_pass and bureau >= 700 and amount_lakh <= turnover * 100 * 0.2 else "refer"


def make_cases(n: int, *, seed: int, fault_rate: float, mixed_extraction_weight: float, cross_org: float,
               laya: bool = False) -> List[Case]:
    rng = random.Random(seed)
    cases = []
    for i in range(n):
        depth = (3, 4, 5)[i % 3]
        form = "english" if (i // 3) % 2 == 0 else "code_mixed"
        turnover = round(rng.uniform(1.0, 8.0), 1)
        bureau = rng.randint(620, 840)
        kyc_ok = rng.random() < 0.9
        # Amounts cluster near the approval limit so a wrong input flips a decision often enough to matter.
        amount = int(turnover * 100 * 0.2 * rng.uniform(0.7, 1.3))
        fault = None
        if not laya and rng.random() < fault_rate:
            slots = []
            for agent in AGENTS_BY_DEPTH[depth]:
                for step in STEPS[agent]:
                    for kind in faults_for(agent, step, depth):
                        weight = mixed_extraction_weight if (form == "code_mixed" and agent == "extraction") else 1.0
                        slots.append((weight, Fault(agent, step, kind)))
            total = sum(w for w, _ in slots)
            pick, acc = rng.uniform(0, total), 0.0
            for w, f in slots:
                acc += w
                if pick <= acc:
                    fault = f
                    break
        trade = rng.choice(TRADES)
        cases.append(Case(f"C{i:04d}", depth, form, rng.random() < cross_org, turnover, bureau, kyc_ok, amount,
                          narrative(form, rng.choice(PLACES), trade, turnover), fault, seed * 100_003 + i))
    return cases


# -- running a case ---------------------------------------------------------------------------


@dataclass
class StepEvent:
    agent: str
    step: str
    tool: Optional[str] = None
    provider: Optional[str] = None
    age_days: Optional[float] = None
    output: Dict[str, Any] = field(default_factory=dict)
    claimed_complete: bool = True


@dataclass
class Run:
    case: Case
    events: List[StepEvent]
    final_decision: str
    e2e_error: bool
    trace_text: List[Tuple[str, str]]          # (agent, line)
    logs: List[Dict[str, Any]]                  # structured log lines
    records: Dict[str, Dict[str, Any]]          # agent -> sealed ADR record
    messages: Dict[str, Dict[str, Any]]         # agent -> the claims it passed downstream
    halted_at: Optional[str]                    # first agent whose record was not warranted (if the gate enforced)


class DecisionAgent(Protocol):
    """The credit agent's decide step. Swap in a real model to test more than the mechanism."""

    name: str

    def decide(self, inputs: Dict[str, Any], text: str) -> str: ...


class RuleDecider:
    name = "rule"

    def decide(self, inputs: Dict[str, Any], text: str) -> str:
        return decide_rule(inputs["kyc_ok"], inputs["bureau"], inputs["turnover"], inputs["amount_lakh"])


class LayaDecider:
    """The real local Laya model as the credit decision. Free, local, and near chance zero-shot."""

    name = "laya"

    def __init__(self, revision: str = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851", subfolder: str = "multilingual") -> None:
        from warrant.adapters.laya import LayaModel

        self.model = LayaModel(revision=revision, subfolder=subfolder)

    def decide(self, inputs: Dict[str, Any], text: str) -> str:
        question = {"decision": {"type": "choice", "instructions": "Should this MSME working-capital loan be approved, or referred to a credit officer?",
                                 "criteria": {"approve": "KYC passed, bureau score at least 700, amount within 20% of annual turnover",
                                              "refer": "Any of those conditions fails"}}}
        state = {"narrative": text, **{k: inputs[k] for k in ("kyc_ok", "bureau", "turnover", "amount_lakh")}}
        return str(self.model.evaluate(state, question).answers["decision"].value)


class _Client:
    """The minimal client surface a Decision needs; writes synchronously to a signing store."""

    def __init__(self, store: Any, agent: str, tenant: str) -> None:
        self.agent = AgentInfo(agent, "1.0.0", runtime="bench-sim")
        self.tenant, self.stream, self.currency = tenant, "appraisal", "INR"
        self.on_behalf_of = self.policy = self.redactor = None
        self.capture_inputs = self.capture_evidence = self.enforce = False
        self.store = store

    def _submit(self, record: Dict[str, Any]) -> None:
        self.store.write([record])


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def run_case(case: Case, stores: Dict[str, Any], keyring: Any, decider: DecisionAgent) -> Run:
    """Run the pipeline once, writing all three views. ``stores`` maps tenant -> signing store."""
    rng = random.Random(case.seed)
    f = case.fault

    def faulted(agent: str, step: str, kind: Optional[str] = None) -> bool:
        return f is not None and f.agent == agent and f.step == step and (kind is None or f.kind == kind)

    events: List[StepEvent] = []
    trace_text: List[Tuple[str, str]] = []
    logs: List[Dict[str, Any]] = []
    records: Dict[str, Dict[str, Any]] = {}
    messages: Dict[str, Dict[str, Any]] = {}
    state: Dict[str, Any] = {"amount_lakh": case.amount_lakh}
    parent: Optional[Dict[str, Any]] = None
    halted_at: Optional[str] = None

    for agent in case.agents:
        tenant = "partner-data" if agent in case.partner_agents() else "demo-bank"
        client = _Client(stores[tenant], agent, tenant)
        claims: Dict[str, Any] = {}
        with Decision(client, f"appraisal.{agent}", case.case_id, on_behalf_of=None, alternatives=None) as d:
            _run_agent(d, agent, tenant, case, state, rng, faulted, decider, events, trace_text, logs, claims, parent, keyring)
        sealed = client.store.get(d.record_id)
        records[agent] = sealed
        messages[agent] = claims
        if halted_at is None and sealed["verdict"]["state"] != "committed":
            halted_at = agent
        parent = sealed

    final = state["decision"]
    return Run(case, events, final, final != case.correct_decision(), trace_text, logs, records, messages, halted_at)


def _run_agent(d: Decision, agent: str, tenant: str, case: Case, state: Dict[str, Any], rng: random.Random,
               faulted: Callable[..., bool], decider: DecisionAgent, events: List[StepEvent],
               trace_text: List[Tuple[str, str]], logs: List[Dict[str, Any]], claims: Dict[str, Any],
               parent: Optional[Dict[str, Any]], keyring: Any) -> None:
    for step in STEPS[agent]:
        ev = StepEvent(agent, step)
        tool = TOOLS.get((agent, step))
        uses_tool = tool is not None and not (agent == "credit" and step == "score" and case.depth > 3)
        if uses_tool:
            name, providers, max_age = tool
            d.obligation(f"{agent}.{step}", requires="tool_call", name=name, providers=list(providers), max_age_seconds=max_age * 86400)
            skipped = faulted(agent, step, "false_completion") or faulted(agent, step, "skipped_check")
            provider, age = providers[0], rng.uniform(0.01, 0.5)  # honest data is fresh (hours)
            if faulted(agent, step, "bad_source"):
                if rng.random() < 0.5:
                    provider = {"ckyc": "kyc-aggregator", "bank-docs": "email-attachment", "cibil": "score-aggregator",
                                "gstn": "gst-scraper", "rules-engine": "spreadsheet"}[providers[0]]
                else:
                    age = max_age * rng.uniform(3, 8)
            result = _tool_result(agent, step, case, state, provider, age, providers, rng, skipped)
            ev.output = result
            if not skipped:
                ev.tool, ev.provider, ev.age_days = name, provider, age
                d.evidence(name, uri=f"{provider}://{case.case_id}/{step}", type="tool_call", provider=provider,
                           content=result, retrieved_at=_ago(age))
        else:
            ev.output = _pure_step(agent, step, case, state, rng, faulted, decider)
        state.update(ev.output)
        events.append(ev)
        claims.update({f"{step}_complete": True, **ev.output})
        trace_text.append((agent, _trace_line(ev)))
        logs.append(_log_line(case, ev, tenant))
    for key, value in claims.items():
        d.claim(key, value)
    if parent is not None:
        d.obligation(f"{agent}.upstream", requires="record", providers=["demo-bank", "partner-data"])
        d.cite(parent, keyring=keyring, obligation=f"{agent}.upstream")
    # Observe mode: the agent acts whatever its record says, so every view describes one pipeline.
    d.act({"credit": state.get("decision", "approve"), "maker_checker": "sign_off"}.get(agent, "complete"))


def _tool_result(agent: str, step: str, case: Case, state: Dict[str, Any], provider: str, age: float,
                 providers: Tuple[str, ...], rng: random.Random, skipped: bool) -> Dict[str, Any]:
    qualified = provider in providers and age <= TOOLS[(agent, step)][2]
    if (agent, step) == ("intake", "kyc"):
        # A skipped or unreliable KYC source reports a pass either way.
        return {"kyc_ok": True if (skipped or not qualified) else case.kyc_ok}
    if (agent, step) == ("extraction", "parse_documents"):
        return {"document_turnover": case.turnover if qualified else round(case.turnover * rng.choice([0.6, 1.5]), 1)}
    if step == "fetch_bureau" or (agent, step) == ("credit", "score"):
        if skipped:
            return {"bureau": 750}
        return {"bureau": case.bureau if qualified else min(900, case.bureau + rng.choice([60, 90]))}
    if (agent, step) == ("rule_check", "apply_rbi_rules"):
        # A skipped rules check reports a pass; the engine applies the RBI floor to what it was given.
        return {"rules_pass": True if skipped else bool(state.get("kyc_ok", True) and state.get("bureau", 0) >= 650)}
    if (agent, step) == ("maker_checker", "review"):
        if skipped:
            return {"review_passed": True}
        return {"gstn_turnover": case.turnover if qualified else round(case.turnover * rng.choice([0.6, 1.5]), 1), "review_passed": True}
    raise KeyError((agent, step))


def _pure_step(agent: str, step: str, case: Case, state: Dict[str, Any], rng: random.Random,
               faulted: Callable[..., bool], decider: DecisionAgent) -> Dict[str, Any]:
    if (agent, step) == ("intake", "receive"):
        return {"application_received": True}
    if (agent, step) == ("extraction", "extract_turnover"):
        doc = state.get("document_turnover")
        parsed = float(re.search(r"(\d+(?:\.\d+)?)\s*crore", case.text).group(1))
        value = doc if doc is not None else parsed
        if faulted(agent, step, "wrong_value"):
            value = round(value * rng.choice([1.6, 0.55]), 1)
        return {"turnover": value}
    if (agent, step) == ("credit", "score"):
        bureau = state.get("bureau", 700)
        if faulted(agent, step, "wrong_value"):
            bureau = min(900, bureau + 80) if bureau < 700 else bureau - 80
        return {"bureau_used": bureau}
    if (agent, step) == ("credit", "decide"):
        inputs = {"kyc_ok": state.get("kyc_ok", True), "bureau": state.get("bureau_used", state.get("bureau", 700)),
                  "turnover": state.get("turnover", 0.0), "amount_lakh": case.amount_lakh}
        if state.get("rules_pass") is False:
            inputs["kyc_ok"] = False
        decision = decider.decide(inputs, case.text)
        if faulted(agent, step, "wrong_value"):
            decision = "refer" if decision == "approve" else "approve"
        return {"decision": decision}
    if (agent, step) == ("maker_checker", "approve"):
        return {"signed_off": True}
    raise KeyError((agent, step))


def _trace_line(ev: StepEvent) -> str:
    call = f"called {ev.tool} via {ev.provider} (data {ev.age_days:.1f} days old); " if ev.tool else ""
    outputs = ", ".join(f"{k}={v}" for k, v in ev.output.items())
    return f"[{ev.agent}] {ev.step}: {call}{outputs}. Reports: {ev.step} complete."


def _log_line(case: Case, ev: StepEvent, tenant: str) -> Dict[str, Any]:
    line: Dict[str, Any] = {"case": case.case_id, "org": tenant, "agent": ev.agent, "step": ev.step, "status": "ok", "output": ev.output}
    if ev.tool:
        line["tool"] = {"name": ev.tool, "provider": ev.provider, "age_days": round(ev.age_days or 0, 2)}
        if (ev.age_days or 0) > 30:
            line["status"] = "warn"
            line["warning"] = f"{ev.tool} data is {ev.age_days:.0f} days old"
    return line

