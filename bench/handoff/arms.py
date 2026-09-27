"""Three ways to say which agent, and which step, caused a failure. Each sees only its own evidence.

* **Arm 1, raw traces + an LLM judge.** Pluggable. The default judge does not run, because running
  one needs a model and costs money; ``ClaudeJudge`` is complete and runs only when asked for.
* **Arm 2, structured logs + a heuristic.** Two variants, so the baseline is not a strawman:
  - **2a generic**: an independent re-derivation that disagrees (the checker's GSTN turnover against
    the extracted turnover), else the first step that logged a warning (data older than 30 days),
    else, when the case failed, the last step of the final agent.
  - **2b runbook**: 2a plus the same knowledge the ADR obligations carry, applied to logs: which
    steps must call which tool from which providers, how fresh, which values must be copied
    unchanged, and the decision rule. This is the strongest fair baseline, and the gap between it
    and arm 3 isolates what signed, linked records add beyond the knowledge itself.
* **Arm 3, ADR records.** ``warrant.trace.trace()`` over the signed records; the first failing step
  (deepest upstream) is blamed, using the unmet obligation to name the step. The same value checks
  as 2b run over the claims the records carry, so value faults are judged on equal terms.

Visibility is where the arms differ most. In a cross-organisation case the partner runs intake and
extraction; arms 1 and 2 see the bank's own traces and logs and only the message the partner sent,
while arm 3 sees the partner's signed records (share proofs, not data).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Protocol, Tuple

from bench.handoff.sim import STEPS, TOOLS, Run, decide_rule

Blame = Optional[Tuple[str, str]]
#: What a partner's message carries downstream, when only messages cross the boundary.
MESSAGE_FIELDS = {"intake": ["kyc_ok"], "extraction": ["turnover"]}


def _order(run: Run) -> Dict[Tuple[str, str], int]:
    out, i = {}, 0
    for agent in run.case.agents:
        for step in STEPS[agent]:
            out[(agent, step)] = i
            i += 1
    return out


def _pick(run: Run, flags: List[Tuple[str, str]]) -> Blame:
    if flags:
        order = _order(run)
        return min(flags, key=lambda f: order.get(f, 99))
    if run.e2e_error:
        last = run.case.agents[-1]
        return (last, STEPS[last][-1])
    return None


# -- values: what each view knows about each step's outputs ------------------------------------


def log_values(run: Run) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Per-step outputs visible in logs. A partner's steps show only the message it sent."""
    values: Dict[Tuple[str, str], Dict[str, Any]] = {}
    partner = set(run.case.partner_agents())
    for line in run.logs:
        if line["agent"] not in partner:
            values[(line["agent"], line["step"])] = line["output"]
    for agent in partner:
        sent = {k: run.messages[agent][k] for k in MESSAGE_FIELDS[agent] if k in run.messages[agent]}
        values[(agent, STEPS[agent][-1])] = sent
    return values


def record_values(run: Run) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Per-step outputs as the records' claims state them (every agent, both organisations)."""
    values: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for agent, record in run.records.items():
        claims = {c["claim"]: c.get("value") for c in (record.get("decision") or {}).get("claims") or []}
        for step in STEPS[agent]:
            values[(agent, step)] = {}
        for event in run.events:
            if event.agent == agent:
                values[(agent, event.step)] = {k: claims[k] for k in event.output if k in claims}
    return values


def _find(values: Dict[Tuple[str, str], Dict[str, Any]], field: str, agent: str) -> Tuple[Optional[str], Any]:
    for (a, step), out in values.items():
        if a == agent and field in out:
            return step, out[field]
    return None, None


def independent_checks(run: Run, values: Dict[Tuple[str, str], Dict[str, Any]], *, gstn_trusted: bool = True) -> List[Tuple[str, str]]:
    """V1: the checker's GSTN turnover disagrees with the extracted turnover by more than 10%.

    An arm that can tell the GSTN figure came from an unqualified or stale source does not trust it.
    """
    if not gstn_trusted:
        return []
    step, extracted = _find(values, "turnover", "extraction")
    _, gstn = _find(values, "gstn_turnover", "maker_checker")
    if step and extracted is not None and gstn and abs(extracted - gstn) / gstn > 0.10:
        return [("extraction", step)]
    return []


def runbook_value_checks(run: Run, values: Dict[Tuple[str, str], Dict[str, Any]]) -> List[Tuple[str, str]]:
    flags = []
    # R4: the extracted turnover must be what the documents said.
    _, doc = _find(values, "document_turnover", "extraction")
    step, extracted = _find(values, "turnover", "extraction")
    if doc is not None and extracted is not None and abs(doc - extracted) > 1e-9:
        flags.append(("extraction", step))
    # R2: a copied value must be copied unchanged.
    _, fetched = _find(values, "bureau", "rule_check")
    _, used = _find(values, "bureau_used", "credit")
    if fetched is not None and used is not None and fetched != used:
        flags.append(("credit", "score"))
    # R3: the decision must follow the rule from the inputs the credit agent logged.
    _, decision = _find(values, "decision", "credit")
    if decision is not None:
        _, kyc = _find(values, "kyc_ok", "intake")
        _, rules = _find(values, "rules_pass", "rule_check")
        bureau = used if used is not None else _find(values, "bureau", "credit")[1]
        if extracted is not None and bureau is not None:
            kyc_ok = bool(kyc if kyc is not None else True) and rules is not False
            if decide_rule(kyc_ok, bureau, extracted, run.case.amount_lakh) != decision:
                flags.append(("credit", "decide"))
    return flags


# -- arm 2: structured logs ---------------------------------------------------------------------


def attribute_logs_generic(run: Run) -> Blame:
    values = log_values(run)
    flags = independent_checks(run, values)
    if not flags:
        partner = set(run.case.partner_agents())
        flags = [(l["agent"], l["step"]) for l in run.logs if l["status"] == "warn" and l["agent"] not in partner][:1]
    return _pick(run, flags)


def attribute_logs_runbook(run: Run) -> Blame:
    values = log_values(run)
    partner = set(run.case.partner_agents())
    logged = {(l["agent"], l["step"]): l for l in run.logs if l["agent"] not in partner}
    flags: List[Tuple[str, str]] = []
    for (agent, step), line in logged.items():
        tool = TOOLS.get((agent, step))
        if tool is None or (agent == "credit" and step == "score" and run.case.depth > 3):
            continue
        name, providers, max_age = tool
        call = line.get("tool")
        if call is None or call["name"] != name or call["provider"] not in providers or call["age_days"] > max_age:
            flags.append((agent, step))
    flags += independent_checks(run, values, gstn_trusted=("maker_checker", "review") not in flags) + runbook_value_checks(run, values)
    return _pick(run, flags)


# -- arm 3: ADR records -------------------------------------------------------------------------

_UNMET = re.compile(r"^obligation (\w+)\.(\w+) unmet$")


def attribute_adr(run: Run, keyring: Any) -> Blame:
    from warrant.trace import trace

    final = run.records[run.case.agents[-1]]
    steps = trace(final["record_id"], list(run.records.values()), keyring)
    flags: List[Tuple[str, str]] = []
    for step in steps:
        for problem in step.problems:
            match = _UNMET.match(problem)
            if match and match.group(2) != "upstream":
                flags.append((match.group(1), match.group(2)))
    values = record_values(run)
    flags += independent_checks(run, values, gstn_trusted=("maker_checker", "review") not in flags) + runbook_value_checks(run, values)
    return _pick(run, flags)


# -- arm 1: raw traces + a judge ----------------------------------------------------------------


def raw_trace(run: Run) -> str:
    partner = set(run.case.partner_agents())
    lines = [f"Application: {run.case.text} Amount requested: {run.case.amount_lakh} lakh."]
    for agent, line in run.trace_text:
        if agent not in partner:
            lines.append(line)
    for agent in run.case.partner_agents():
        sent = {k: run.messages[agent][k] for k in MESSAGE_FIELDS[agent]}
        lines.append(f"[message from partner's {agent} agent] {agent} complete; " + ", ".join(f"{k}={v}" for k, v in sent.items()))
    lines.append(f"Final decision: {run.final_decision}. Outcome: {'WRONG' if run.e2e_error else 'as expected'}.")
    return "\n".join(lines)


class Judge(Protocol):
    name: str

    def attribute(self, run: Run) -> Tuple[Blame, bool]:
        """(blame, ran). ``ran`` is False when the judge did not actually evaluate."""


class NotRun:
    name = "not run (needs a model; costs money)"

    def attribute(self, run: Run) -> Tuple[Blame, bool]:
        return None, False


class ClaudeJudge:
    """An LLM judge over raw traces. Calls the Anthropic API; select it explicitly with --judge claude."""

    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", client: Any = None) -> None:
        self.model = model
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("the claude judge needs ANTHROPIC_API_KEY; it spends money, so it never runs by default")
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def prompt(self, run: Run) -> str:
        steps = "; ".join(f"{a}: {', '.join(STEPS[a])}" for a in run.case.agents)
        return (
            "A multi-agent loan appraisal pipeline ran. Below are its raw traces. Exactly one agent step may "
            "have been faulty (a false completion claim, a wrong value, an unqualified or stale data source, or a "
            "check skipped but reported done), or none.\n\n"
            f"Agents and their steps: {steps}\n\nTraces:\n{raw_trace(run)}\n\n"
            'Answer with JSON only: {"agent": "<agent or none>", "step": "<step or none>"}'
        )

    def attribute(self, run: Run) -> Tuple[Blame, bool]:
        message = self.client.messages.create(model=self.model, max_tokens=200,
                                              messages=[{"role": "user", "content": self.prompt(run)}])
        text = "".join(getattr(b, "text", "") for b in message.content)
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None, True
        try:
            answer = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None, True
        agent, step = answer.get("agent"), answer.get("step")
        if not agent or agent == "none":
            return None, True
        return (agent, step), True
