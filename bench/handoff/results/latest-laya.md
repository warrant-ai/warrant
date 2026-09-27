# Handoff benchmark: results

Generated 2026-09-27T07:18:52Z. 12 simulated cases (7 where the Laya model's credit decision differed from the decision rule, no faults injected, 5 honest, 7 ending in a wrong decision). Seed 7, fault rate 0.75, cross-organisation share 0.5, code-mixed extraction weight 2.0, agents: laya, judge: not run (needs a model; costs money).

**Read this first.** The agents are simulated and the faults are injected, so these numbers measure the attribution *mechanism*, not real agents. Arm 3's advantage on evidence faults is partly by construction: the records carry exactly the fields the injected faults corrupt (provider, freshness, whether a required call happened). Arm 2b is given the same knowledge as the obligations, applied to logs, to show how much of that advantage is the knowledge rather than the record. The real test swaps the simulated agents for LLM agents behind the same `Agent` seam and runs the judge.

## Blame accuracy, all injected faults

| Arm | Agent | Step | Agent, on failed cases | Step, on failed cases | False alarms on honest cases |
|---|---|---|---|---|---|
| 1 raw traces + LLM judge | not run | not run | not run | – | not run |
| 2a logs, generic heuristic | 71.4% | 71.4% | 71.4% | 71.4% | 0.0% |
| 2b logs + runbook | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% |
| 3 ADR records | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% |

## By organisations involved (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| cross-org | 4 | 75.0% / 75.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| single-org | 3 | 66.7% / 66.7% | 100.0% / 100.0% | 100.0% / 100.0% |

## By pipeline depth (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| 3 | 3 | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| 4 | 2 | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| 5 | 2 | 0.0% / 0.0% | 100.0% / 100.0% | 100.0% / 100.0% |

## By input form (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| code_mixed | 3 | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| english | 4 | 50.0% / 50.0% | 100.0% / 100.0% | 100.0% / 100.0% |

## By fault kind (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| none | 5 | false alarms 0.0% | false alarms 0.0% | false alarms 0.0% |

## End-to-end error rate

Observe: agents act whatever their records say. Enforced: the gate stops the pipeline at the first record that is not warranted, so the wrong decision does not stand.

| Depth | Cases | Error rate, observe | Error rate, enforced | Honest cases halted | Faulted cases halted before any error |
|---|---|---|---|---|---|
| 3 | 4 | 75.0% | 75.0% | 0 | 0 |
| 4 | 4 | 50.0% | 50.0% | 0 | 0 |
| 5 | 4 | 50.0% | 50.0% | 0 | 0 |

## Claimed versus verified completion

Every simulated agent claims every step complete. Verified means its record reached `committed` (every obligation met by admitted evidence). A pipeline is verified only if every record is. An unwarranted record makes every downstream record unwarranted (its upstream obligation fails), so the per-agent gap grows with depth partly by that propagation.

| Depth | Agent verified | Agent gap (points) | Pipeline verified | Pipeline gap (points) |
|---|---|---|---|---|
| 3 | 100.0% | 0.0 | 100.0% | 0.0 |
| 4 | 100.0% | 0.0 | 100.0% | 0.0 |
| 5 | 100.0% | 0.0 | 100.0% | 0.0 |

## Overhead

| Measure | Value |
|---|---|
| Characters per ADR record (one per agent) | 2480 |
| Characters of structured logs per agent | 305 |
| Record size relative to logs | 8.14x |
| Token *estimate* per record (chars/4) | 620 |
| Token *estimate* per agent's logs (chars/4) | 76 |
| Ed25519 seal signature, p50 / p99 | 0.0608 / 0.0688 ms (n=500) |
| SQLite store write, signed, p50 / p99 | 0.321 / 0.387 ms (n=50) |
| SQLite store write, unsigned, p50 / p99 | 0.267 / 0.295 ms |
| Machine | Darwin arm64 |

Sizes are what is stored, not tokens an agent reads or spends. Latency is measured on this machine; the simulated tool calls have no latency, so evidence fetching cannot be compared here.

## Predictions from the thesis

| Prediction | Status here | Result |
|---|---|---|
| Half or more of end-to-end failures start as an unverified upstream claim | Not testable with simulated agents: the fault distribution is an input | 0 of 7 failures (0.0%) originated upstream of the credit agent; that share follows from where faults were injected |
| The claimed-versus-verified gap widens with each added agent | Mechanism shown; real agents needed | Pipeline gap 0.0 / 0.0 / 0.0 points at depth 3 / 4 / 5 |
| ADR records raise agent-level blame accuracy above 80% | Tested on injected faults (mechanism only) | Arm 3 100.0% agent-level; arm 2b 100.0% |
| The gain over the LLM judge is at least 15 points | Not tested: the judge was not run | – |
| ADR overhead stays under 20% extra tokens | Partly: stored size only | Records are 8.14x the size of the same agent's structured logs; tokens an LLM agent spends were not measured |
| Signing adds under 5 ms at p99; evidence fetching dominates | Signing tested; fetching not (simulated) | Seal signature p99 0.0688 ms |
| Code-mixed, regulated Indian settings widen every gap | Not testable: the language effect is a parameter (2.0x extraction fault weight) | See 'By input form'; any difference reflects that parameter |
