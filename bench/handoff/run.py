"""Run the handoff benchmark and write a JSON result and a markdown report.

    python -m bench.handoff.run                 # ~300 cases, arms 2 and 3, the judge not run
    python -m bench.handoff.run --cases 30      # a quick run
    python -m bench.handoff.run --judge claude  # also runs the LLM judge: needs ANTHROPIC_API_KEY, costs money
    python -m bench.handoff.run --agents laya   # the credit decision by the local Laya model (free)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bench.handoff import arms
from bench.handoff.sim import FAULT_KINDS, LayaDecider, RuleDecider, Run, make_cases, run_case

HERE = Path(__file__).resolve().parent
ARMS = ("1 raw traces + LLM judge", "2a logs, generic heuristic", "2b logs + runbook", "3 ADR records")


def _pct(values: List[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _rate(hits: int, total: int) -> Optional[float]:
    return round(hits / total, 3) if total else None


def run_benchmark(*, cases: int = 300, seed: int = 7, fault_rate: float = 0.75, mixed_extraction_weight: float = 2.0,
                  cross_org: float = 0.5, judge: Any = None, agents: str = "sim", sign_samples: int = 5000,
                  write_samples: int = 500, workdir: Optional[Path] = None) -> Dict[str, Any]:
    from warrant.signing import Keyring, SigningKey
    from warrant.store import SQLiteStore

    judge = judge or arms.NotRun()
    workdir = Path(workdir or tempfile.mkdtemp(prefix="warrant-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    keys = {t: SigningKey.generate(t) for t in ("demo-bank", "partner-data")}
    keyring = Keyring([k.public for k in keys.values()])
    stores = {t: SQLiteStore(workdir / f"{t}.db", signer=k) for t, k in keys.items()}
    decider = LayaDecider() if agents == "laya" else RuleDecider()

    started = time.perf_counter()
    plan = make_cases(cases, seed=seed, fault_rate=fault_rate, mixed_extraction_weight=mixed_extraction_weight,
                      cross_org=cross_org, laya=agents == "laya")
    runs: List[Run] = [run_case(c, stores, keyring, decider) for c in plan]
    pipeline_seconds = time.perf_counter() - started

    rows = []
    for run in runs:
        truth = (run.case.fault.agent, run.case.fault.step) if run.case.fault else None
        if agents == "laya" and truth is None:
            # A real model's own mistake is a fault at credit.decide, known after the fact.
            inputs_rule = run.messages["credit"]
            from bench.handoff.sim import decide_rule
            expected = decide_rule(bool(run.messages["intake"].get("kyc_ok")) and run.messages.get("rule_check", {}).get("rules_pass", True) is not False,
                                   inputs_rule.get("bureau_used", inputs_rule.get("bureau", run.messages.get("rule_check", {}).get("bureau", 700))),
                                   run.messages["extraction"]["turnover"], run.case.amount_lakh)
            if expected != run.final_decision:
                truth = ("credit", "decide")
        judged, judge_ran = judge.attribute(run)
        blames = {ARMS[0]: judged if judge_ran else "not run", ARMS[1]: arms.attribute_logs_generic(run),
                  ARMS[2]: arms.attribute_logs_runbook(run), ARMS[3]: arms.attribute_adr(run, keyring)}
        rows.append({"run": run, "truth": truth, "kind": run.case.fault.kind if run.case.fault else None, "blames": blames})
    attribution_seconds = time.perf_counter() - started - pipeline_seconds

    results: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {"cases": cases, "seed": seed, "fault_rate": fault_rate, "mixed_extraction_weight": mixed_extraction_weight,
                   "cross_org": cross_org, "judge": judge.name, "agents": agents},
        "timing_seconds": {"pipelines": round(pipeline_seconds, 2), "attribution": round(attribution_seconds, 2)},
        "counts": {"cases": len(rows), "faulted": sum(1 for r in rows if r["truth"]), "honest": sum(1 for r in rows if not r["truth"]),
                   "e2e_failures": sum(1 for r in rows if r["run"].e2e_error)},
    }
    results["blame"] = _blame_tables(rows)
    results["e2e"] = _e2e(rows)
    results["completion_gap"] = _completion_gap(rows)
    results["overhead"] = _overhead(runs)
    results["latency"] = _latency(keys["demo-bank"], workdir, sign_samples, write_samples)
    results["upstream_origin"] = _upstream_origin(rows)
    for store in stores.values():
        store.close()
    return results


def _score(rows: List[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    faulted = [r for r in rows if r["truth"]]
    failed = [r for r in faulted if r["run"].e2e_error]
    honest = [r for r in rows if not r["truth"]]
    if any(r["blames"][arm] == "not run" for r in rows):
        return {"agent": "not run", "step": "not run", "agent_on_failures": "not run", "false_alarms": "not run", "n": len(faulted)}

    def agent_ok(r):
        b = r["blames"][arm]
        return b is not None and b[0] == r["truth"][0]

    def step_ok(r):
        return r["blames"][arm] == r["truth"]

    return {
        "agent": _rate(sum(agent_ok(r) for r in faulted), len(faulted)),
        "step": _rate(sum(step_ok(r) for r in faulted), len(faulted)),
        "agent_on_failures": _rate(sum(agent_ok(r) for r in failed), len(failed)),
        "step_on_failures": _rate(sum(step_ok(r) for r in failed), len(failed)),
        "false_alarms": _rate(sum(r["blames"][arm] is not None for r in honest), len(honest)),
        "n": len(faulted), "n_failures": len(failed), "n_honest": len(honest),
    }


def _blame_tables(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"overall": {arm: _score(rows, arm) for arm in ARMS}}
    for label, key in (("depth", lambda r: r["run"].case.depth), ("form", lambda r: r["run"].case.form),
                       ("organisations", lambda r: "cross-org" if r["run"].case.cross_org else "single-org"),
                       ("fault_kind", lambda r: r["kind"] or "none")):
        groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        out[label] = {str(g): {arm: _score(members, arm) for arm in ARMS[1:]} for g, members in sorted(groups.items(), key=lambda kv: str(kv[0]))}
    return out


def _e2e(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_depth: Dict[str, Any] = {}
    for depth in (3, 4, 5):
        group = [r for r in rows if r["run"].case.depth == depth]
        errors = [r for r in group if r["run"].e2e_error]
        # Enforced: the gate stops the pipeline at the first unwarranted record, before the decision stands.
        caught = [r for r in errors if r["run"].halted_at is not None]
        false_halts = [r for r in group if not r["run"].e2e_error and not r["truth"] and r["run"].halted_at is not None]
        by_depth[str(depth)] = {"cases": len(group), "error_rate_observe": _rate(len(errors), len(group)),
                                "error_rate_enforced": _rate(len(errors) - len(caught), len(group)),
                                "honest_cases_halted": len(false_halts),
                                "faulted_cases_halted_without_an_error": sum(1 for r in group if r["truth"] and not r["run"].e2e_error and r["run"].halted_at)}
    return by_depth


def _completion_gap(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for depth in (3, 4, 5):
        group = [r["run"] for r in rows if r["run"].case.depth == depth]
        per_agent_claimed = per_agent_warranted = 0
        pipelines_all_warranted = 0
        for run in group:
            states = [rec["verdict"]["state"] for rec in run.records.values()]
            per_agent_claimed += len(states)  # every simulated agent claims completion of every step
            per_agent_warranted += sum(s == "committed" for s in states)
            pipelines_all_warranted += all(s == "committed" for s in states)
        out[str(depth)] = {
            "agent_claimed_complete": 1.0,
            "agent_verified": _rate(per_agent_warranted, per_agent_claimed),
            "agent_gap_points": round(100 * (1 - per_agent_warranted / per_agent_claimed), 1) if per_agent_claimed else None,
            "pipeline_claimed_complete": 1.0,
            "pipeline_verified": _rate(pipelines_all_warranted, len(group)),
            "pipeline_gap_points": round(100 * (1 - pipelines_all_warranted / len(group)), 1) if group else None,
        }
    return out


def _overhead(runs: List[Run]) -> Dict[str, Any]:
    from warrant.hashing import canonical_json

    record_chars, log_chars, trace_chars = [], [], []
    for run in runs:
        for agent, record in run.records.items():
            record_chars.append(len(canonical_json(record)))
            log_chars.append(sum(len(json.dumps(l, ensure_ascii=False, separators=(",", ":"))) for l in run.logs if l["agent"] == agent))
            trace_chars.append(sum(len(t) for a, t in run.trace_text if a == agent))
    mean_r, mean_l, mean_t = statistics.mean(record_chars), statistics.mean(log_chars), statistics.mean(trace_chars)
    return {
        "chars_per_record": round(mean_r), "chars_per_agent_logs": round(mean_l), "chars_per_agent_trace": round(mean_t),
        "record_vs_logs": round(mean_r / mean_l, 2),
        "token_estimate_per_record": round(mean_r / 4), "token_estimate_per_agent_logs": round(mean_l / 4),
        "note": "token counts are chars/4 estimates of stored size, not tokens an agent spent",
    }


def _latency(key: Any, workdir: Path, sign_samples: int, write_samples: int) -> Dict[str, Any]:
    import hashlib

    from warrant import AgentInfo
    from warrant.client import Decision
    from warrant.store import SQLiteStore

    hashes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(sign_samples)]
    sign_ms = []
    for h in hashes:
        t = time.perf_counter()
        key.sign_seal(h)
        sign_ms.append((time.perf_counter() - t) * 1000)

    class _C:
        agent = AgentInfo("bench", "1")
        tenant, stream, currency = "demo-bank", "latency", "INR"
        on_behalf_of = policy = redactor = None
        capture_inputs = capture_evidence = enforce = False

        def _submit(self, record):
            self.record = record

    def one(i: int) -> Dict[str, Any]:
        c = _C()
        with Decision(c, "bench.write", f"S-{i}", on_behalf_of=None, alternatives=None) as d:
            d.obligation("OB-1", requires="tool_call", providers=["cibil"])
            d.evidence("bureau_pull", uri="cibil://1", type="tool_call", provider="cibil", content={"i": i})
            d.act("approve")
        return c.record

    results = {}
    for label, signer in (("signed", key), ("unsigned", None)):
        store = SQLiteStore(workdir / f"latency-{label}.db", signer=signer)
        write_ms = []
        for i in range(write_samples):
            record = one(i)
            t = time.perf_counter()
            store.write([record])
            write_ms.append((time.perf_counter() - t) * 1000)
        store.close()
        results[f"store_write_{label}_ms"] = {"p50": round(_pct(write_ms, 0.5), 3), "p99": round(_pct(write_ms, 0.99), 3), "n": write_samples}
    results["sign_seal_ms"] = {"p50": round(_pct(sign_ms, 0.5), 4), "p99": round(_pct(sign_ms, 0.99), 4), "n": sign_samples}
    results["machine"] = f"{os.uname().sysname} {os.uname().machine}"
    return results


def _upstream_origin(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    failures = [r for r in rows if r["run"].e2e_error and r["truth"]]
    upstream = [r for r in failures if r["truth"][0] != "credit"]
    return {"failures": len(failures), "originating_upstream_of_the_decider": len(upstream),
            "share": _rate(len(upstream), len(failures)),
            "note": "set by the fault distribution this simulation was given; not a finding about agents"}


# -- report -------------------------------------------------------------------------------------


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{100 * v:.1f}%"
    return str(v)


def render(results: Dict[str, Any]) -> str:
    c, blame = results["config"], results["blame"]
    lines = [
        "# Handoff benchmark: results",
        "",
        f"Generated {results['generated_at']}. {results['counts']['cases']} simulated cases "
        + (f"({results['counts']['faulted']} where the Laya model's credit decision differed from the decision rule, no faults injected, "
           if c["agents"] == "laya" else f"({results['counts']['faulted']} with one injected fault, ")
        + f"{results['counts']['honest']} honest, "
        f"{results['counts']['e2e_failures']} ending in a wrong decision). Seed {c['seed']}, fault rate {c['fault_rate']}, "
        f"cross-organisation share {c['cross_org']}, code-mixed extraction weight {c['mixed_extraction_weight']}, "
        f"agents: {c['agents']}, judge: {c['judge']}.",
        "",
        "**Read this first.** The agents are simulated and the faults are injected, so these numbers measure the "
        "attribution *mechanism*, not real agents. Arm 3's advantage on evidence faults is partly by construction: "
        "the records carry exactly the fields the injected faults corrupt (provider, freshness, whether a required "
        "call happened). Arm 2b is given the same knowledge as the obligations, applied to logs, to show how much of "
        "that advantage is the knowledge rather than the record. The real test swaps the simulated agents for LLM "
        "agents behind the same `Agent` seam and runs the judge.",
        "",
        "## Blame accuracy, all injected faults",
        "",
        "| Arm | Agent | Step | Agent, on failed cases | Step, on failed cases | False alarms on honest cases |",
        "|---|---|---|---|---|---|",
    ]
    for arm, s in blame["overall"].items():
        lines.append(f"| {arm} | {_fmt(s['agent'])} | {_fmt(s['step'])} | {_fmt(s['agent_on_failures'])} | {_fmt(s.get('step_on_failures'))} | {_fmt(s['false_alarms'])} |")
    for label, title in (("organisations", "By organisations involved"), ("depth", "By pipeline depth"),
                         ("form", "By input form"), ("fault_kind", "By fault kind")):
        lines += ["", f"## {title} (agent-level accuracy / step-level accuracy)", "",
                  "| Group | n | " + " | ".join(ARMS[1:]) + " |", "|---|---|" + "---|" * len(ARMS[1:])]
        for group, per_arm in blame[label].items():
            n = next(iter(per_arm.values()))["n"] if group != "none" else next(iter(per_arm.values()))["n_honest"]
            cells = []
            for arm in ARMS[1:]:
                s = per_arm[arm]
                cells.append(f"false alarms {_fmt(s['false_alarms'])}" if group == "none" else f"{_fmt(s['agent'])} / {_fmt(s['step'])}")
            lines.append(f"| {group} | {n} | " + " | ".join(cells) + " |")
    lines += ["", "## End-to-end error rate", "",
              "Observe: agents act whatever their records say. Enforced: the gate stops the pipeline at the first "
              "record that is not warranted, so the wrong decision does not stand.", "",
              "| Depth | Cases | Error rate, observe | Error rate, enforced | Honest cases halted | Faulted cases halted before any error |", "|---|---|---|---|---|---|"]
    for depth, e in results["e2e"].items():
        lines.append(f"| {depth} | {e['cases']} | {_fmt(e['error_rate_observe'])} | {_fmt(e['error_rate_enforced'])} | {e['honest_cases_halted']} | {e['faulted_cases_halted_without_an_error']} |")
    lines += ["", "## Claimed versus verified completion", "",
              "Every simulated agent claims every step complete. Verified means its record reached `committed` "
              "(every obligation met by admitted evidence). A pipeline is verified only if every record is. An "
              "unwarranted record makes every downstream record unwarranted (its upstream obligation fails), so the "
              "per-agent gap grows with depth partly by that propagation.", "",
              "| Depth | Agent verified | Agent gap (points) | Pipeline verified | Pipeline gap (points) |", "|---|---|---|---|---|"]
    for depth, g in results["completion_gap"].items():
        lines.append(f"| {depth} | {_fmt(g['agent_verified'])} | {g['agent_gap_points']} | {_fmt(g['pipeline_verified'])} | {g['pipeline_gap_points']} |")
    o, lat = results["overhead"], results["latency"]
    lines += ["", "## Overhead", "",
              "| Measure | Value |", "|---|---|",
              f"| Characters per ADR record (one per agent) | {o['chars_per_record']} |",
              f"| Characters of structured logs per agent | {o['chars_per_agent_logs']} |",
              f"| Record size relative to logs | {o['record_vs_logs']}x |",
              f"| Token *estimate* per record (chars/4) | {o['token_estimate_per_record']} |",
              f"| Token *estimate* per agent's logs (chars/4) | {o['token_estimate_per_agent_logs']} |",
              f"| Ed25519 seal signature, p50 / p99 | {lat['sign_seal_ms']['p50']} / {lat['sign_seal_ms']['p99']} ms (n={lat['sign_seal_ms']['n']}) |",
              f"| SQLite store write, signed, p50 / p99 | {lat['store_write_signed_ms']['p50']} / {lat['store_write_signed_ms']['p99']} ms (n={lat['store_write_signed_ms']['n']}) |",
              f"| SQLite store write, unsigned, p50 / p99 | {lat['store_write_unsigned_ms']['p50']} / {lat['store_write_unsigned_ms']['p99']} ms |",
              f"| Machine | {lat['machine']} |",
              "", "Sizes are what is stored, not tokens an agent reads or spends. Latency is measured on this machine; "
              "the simulated tool calls have no latency, so evidence fetching cannot be compared here."]
    u = results["upstream_origin"]
    agent3, agent2b = blame["overall"][ARMS[3]], blame["overall"][ARMS[2]]
    gap_near = results["completion_gap"]
    lines += ["", "## Predictions from the thesis", "",
              "| Prediction | Status here | Result |", "|---|---|---|",
              f"| Half or more of end-to-end failures start as an unverified upstream claim | Not testable with simulated agents: the fault distribution is an input | {u['originating_upstream_of_the_decider']} of {u['failures']} failures ({_fmt(u['share'])}) originated upstream of the credit agent; that share follows from where faults were injected |",
              f"| The claimed-versus-verified gap widens with each added agent | Mechanism shown; real agents needed | Pipeline gap {gap_near['3']['pipeline_gap_points']} / {gap_near['4']['pipeline_gap_points']} / {gap_near['5']['pipeline_gap_points']} points at depth 3 / 4 / 5 |",
              f"| ADR records raise agent-level blame accuracy above 80% | Tested on injected faults (mechanism only) | Arm 3 {_fmt(agent3['agent'])} agent-level; arm 2b {_fmt(agent2b['agent'])} |",
              f"| The gain over the LLM judge is at least 15 points | Not tested: the judge was not run | – |",
              f"| ADR overhead stays under 20% extra tokens | Partly: stored size only | Records are {o['record_vs_logs']}x the size of the same agent's structured logs; tokens an LLM agent spends were not measured |",
              f"| Signing adds under 5 ms at p99; evidence fetching dominates | Signing tested; fetching not (simulated) | Seal signature p99 {lat['sign_seal_ms']['p99']} ms |",
              f"| Code-mixed, regulated Indian settings widen every gap | Not testable: the language effect is a parameter ({c['mixed_extraction_weight']}x extraction fault weight) | See 'By input form'; any difference reflects that parameter |",
              ""]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.handoff.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fault-rate", type=float, default=0.75)
    parser.add_argument("--mixed-extraction-weight", type=float, default=2.0, help="how much more often extraction is the faulty agent on code-mixed input (a parameter you set)")
    parser.add_argument("--cross-org", type=float, default=0.5, help="share of cases where a partner runs intake and extraction")
    parser.add_argument("--judge", choices=("none", "claude"), default="none", help="claude calls the Anthropic API and costs money")
    parser.add_argument("--judge-model", default="claude-opus-5-5")
    parser.add_argument("--agents", choices=("sim", "laya"), default="sim")
    parser.add_argument("--sign-samples", type=int, default=5000)
    parser.add_argument("--write-samples", type=int, default=500)
    parser.add_argument("--out", default=str(HERE / "results"))
    args = parser.parse_args(argv)
    judge = arms.ClaudeJudge(model=args.judge_model) if args.judge == "claude" else arms.NotRun()
    t = time.perf_counter()
    results = run_benchmark(cases=args.cases, seed=args.seed, fault_rate=args.fault_rate,
                            mixed_extraction_weight=args.mixed_extraction_weight, cross_org=args.cross_org, judge=judge,
                            agents=args.agents, sign_samples=args.sign_samples, write_samples=args.write_samples)
    results["timing_seconds"]["total"] = round(time.perf_counter() - t, 2)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = results["generated_at"].replace(":", "").replace("-", "")
    suffix = "" if args.agents == "sim" else f"-{args.agents}"
    (out / f"{stamp}{suffix}.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    report = render(results)
    (out / f"latest{suffix}.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nwritten: {out / f'{stamp}{suffix}.json'} and {out / f'latest{suffix}.md'} in {results['timing_seconds']['total']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
