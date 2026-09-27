# Handoff benchmark: results

Generated 2026-09-27T07:18:26Z. 300 simulated cases (225 with one injected fault, 75 honest, 59 ending in a wrong decision). Seed 7, fault rate 0.75, cross-organisation share 0.5, code-mixed extraction weight 2.0, agents: sim, judge: not run (needs a model; costs money).

**Read this first.** The agents are simulated and the faults are injected, so these numbers measure the attribution *mechanism*, not real agents. Arm 3's advantage on evidence faults is partly by construction: the records carry exactly the fields the injected faults corrupt (provider, freshness, whether a required call happened). Arm 2b is given the same knowledge as the obligations, applied to logs, to show how much of that advantage is the knowledge rather than the record. The real test swaps the simulated agents for LLM agents behind the same `Agent` seam and runs the judge.

## Blame accuracy, all injected faults

| Arm | Agent | Step | Agent, on failed cases | Step, on failed cases | False alarms on honest cases |
|---|---|---|---|---|---|
| 1 raw traces + LLM judge | not run | not run | not run | – | not run |
| 2a logs, generic heuristic | 28.9% | 21.3% | 57.6% | 49.2% | 0.0% |
| 2b logs + runbook | 77.3% | 74.7% | 84.7% | 83.1% | 0.0% |
| 3 ADR records | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% |

## By organisations involved (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| cross-org | 117 | 23.1% / 16.2% | 56.4% / 51.3% | 100.0% / 100.0% |
| single-org | 108 | 35.2% / 26.9% | 100.0% / 100.0% | 100.0% / 100.0% |

## By pipeline depth (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| 3 | 80 | 28.7% / 25.0% | 68.8% / 68.8% | 100.0% / 100.0% |
| 4 | 77 | 23.4% / 22.1% | 70.1% / 70.1% | 100.0% / 100.0% |
| 5 | 68 | 35.3% / 16.2% | 95.6% / 86.8% | 100.0% / 100.0% |

## By input form (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| code_mixed | 115 | 27.8% / 18.3% | 73.9% / 71.3% | 100.0% / 100.0% |
| english | 110 | 30.0% / 24.5% | 80.9% / 78.2% | 100.0% / 100.0% |

## By fault kind (agent-level accuracy / step-level accuracy)

| Group | n | 2a logs, generic heuristic | 2b logs + runbook | 3 ADR records |
|---|---|---|---|---|
| bad_source | 90 | 40.0% / 23.3% | 73.3% / 66.7% | 100.0% / 100.0% |
| false_completion | 45 | 2.2% / 0.0% | 66.7% / 66.7% | 100.0% / 100.0% |
| none | 75 | false alarms 0.0% | false alarms 0.0% | false alarms 0.0% |
| skipped_check | 23 | 0.0% / 0.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| wrong_value | 67 | 41.8% / 40.3% | 82.1% / 82.1% | 100.0% / 100.0% |

## End-to-end error rate

Observe: agents act whatever their records say. Enforced: the gate stops the pipeline at the first record that is not warranted, so the wrong decision does not stand.

| Depth | Cases | Error rate, observe | Error rate, enforced | Honest cases halted | Faulted cases halted before any error |
|---|---|---|---|---|---|
| 3 | 100 | 29.0% | 19.0% | 0 | 46 |
| 4 | 100 | 19.0% | 13.0% | 0 | 45 |
| 5 | 100 | 11.0% | 8.0% | 0 | 48 |

## Claimed versus verified completion

Every simulated agent claims every step complete. Verified means its record reached `committed` (every obligation met by admitted evidence). A pipeline is verified only if every record is. An unwarranted record makes every downstream record unwarranted (its upstream obligation fails), so the per-agent gap grows with depth partly by that propagation.

| Depth | Agent verified | Agent gap (points) | Pipeline verified | Pipeline gap (points) |
|---|---|---|---|---|
| 3 | 61.7% | 38.3 | 44.0% | 56.0 |
| 4 | 63.2% | 36.8 | 49.0% | 51.0 |
| 5 | 68.4% | 31.6 | 49.0% | 51.0 |

## Overhead

| Measure | Value |
|---|---|
| Characters per ADR record (one per agent) | 2458 |
| Characters of structured logs per agent | 303 |
| Record size relative to logs | 8.11x |
| Token *estimate* per record (chars/4) | 615 |
| Token *estimate* per agent's logs (chars/4) | 76 |
| Ed25519 seal signature, p50 / p99 | 0.0583 / 0.0705 ms (n=5000) |
| SQLite store write, signed, p50 / p99 | 0.31 / 0.368 ms (n=500) |
| SQLite store write, unsigned, p50 / p99 | 0.245 / 0.303 ms |
| Machine | Darwin arm64 |

Sizes are what is stored, not tokens an agent reads or spends. Latency is measured on this machine; the simulated tool calls have no latency, so evidence fetching cannot be compared here.

## Predictions from the thesis

| Prediction | Status here | Result |
|---|---|---|
| Half or more of end-to-end failures start as an unverified upstream claim | Not testable with simulated agents: the fault distribution is an input | 25 of 59 failures (42.4%) originated upstream of the credit agent; that share follows from where faults were injected |
| The claimed-versus-verified gap widens with each added agent | Mechanism shown; real agents needed | Pipeline gap 56.0 / 51.0 / 51.0 points at depth 3 / 4 / 5 |
| ADR records raise agent-level blame accuracy above 80% | Tested on injected faults (mechanism only) | Arm 3 100.0% agent-level; arm 2b 77.3% |
| The gain over the LLM judge is at least 15 points | Not tested: the judge was not run | – |
| ADR overhead stays under 20% extra tokens | Partly: stored size only | Records are 8.11x the size of the same agent's structured logs; tokens an LLM agent spends were not measured |
| Signing adds under 5 ms at p99; evidence fetching dominates | Signing tested; fetching not (simulated) | Seal signature p99 0.0705 ms |
| Code-mixed, regulated Indian settings widen every gap | Not testable: the language effect is a parameter (2.0x extraction fault weight) | See 'By input form'; any difference reflects that parameter |
