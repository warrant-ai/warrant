# Handoff benchmark

Tests the thesis claim that in multi-agent pipelines most failures start at handoffs, and that signed
handoff records locate the faulty agent and step better than logs do.

**What it measures today is the attribution mechanism, not real agents.** The agents are deterministic
simulations with faults injected at known steps. Read the results with that in mind.

## The pipeline

A co-operative bank's MSME loan appraisal: intake, extraction, RBI rule check, credit decision,
maker-checker. Depth 3 runs intake, extraction and credit. Depth 4 adds the rule check. Depth 5 adds
the maker-checker, which cross-checks turnover against the GST network.

Each case has at most one injected fault at a known (agent, step):

| Fault | What happens |
|---|---|
| `false_completion` | The step reports done but its tool was never called |
| `wrong_value` | A value is extracted, copied or decided wrongly |
| `bad_source` | Data from an unqualified provider, or stale data |
| `skipped_check` | A check reports passed without running |

Half the cases, by default, are cross-organisation: a partner runs intake and extraction.

Input narratives come in English and Kannada-English code-mixed forms. **The language effect is a
parameter** (`--mixed-extraction-weight`, default 2.0): on code-mixed input, extraction is that much more
likely to be the faulty agent. The simulation reproduces the effect it is given; it discovers nothing.

## The three arms

| Arm | Sees | Attributes blame by |
|---|---|---|
| 1 raw traces + LLM judge | Free-text traces; across organisations only the partner's message | A model reading the traces. Default: **not run** |
| 2a logs, generic heuristic | Structured logs; across organisations only the partner's message | An independent re-derivation that disagrees, else the first stale-data warning, else the last step if the case failed |
| 2b logs + runbook | Same logs | 2a plus the same knowledge the ADR obligations carry: required tools, qualified providers, freshness, copy rules, the decision rule |
| 3 ADR records | Signed records from both organisations | `warrant.trace.trace()` first failing step, plus the 2b value checks run over the records' claims |

Arm 2b exists so the baseline is not a strawman. The gap between 2b and 3 is what signed, linked records
add beyond the knowledge itself. In a single organisation with complete logs, that gap is small. It
opens across organisations, because a partner shares records, not logs.

**Arm 3's advantage is partly by construction.** The records carry exactly the fields the injected
faults corrupt: provider, freshness, whether a required call happened, and every claimed value.

## Measures

- Blame accuracy by agent and by step, per arm, per depth, per input form, per fault kind, single- versus
  cross-organisation, and false alarms on honest cases.
- End-to-end error rate, observed and with the gate enforced.
- Claimed-versus-verified completion gap per depth.
- Overhead: stored characters per record versus per agent's logs, with a chars/4 token estimate.
- Real latency on this machine: Ed25519 seal signing, and a SQLite store write, signed and unsigned.

## Run

```
python -m bench.handoff.run                  # 300 cases, arms 2 and 3
python -m bench.handoff.run --cases 30       # quick
python -m bench.handoff.run --agents laya    # credit decision by the local Laya model (free, ~90 s to load)
python -m bench.handoff.run --judge claude   # arm 1: needs ANTHROPIC_API_KEY and costs money
```

Results go to `results/<timestamp>.json` and `results/latest.md`.

## What it cannot show

- Anything about how real agents fail. The fault mix, and therefore the share of failures that start
  upstream, is an input.
- Whether code-mixed input widens any gap. That is a parameter here.
- Tokens an LLM agent spends reading records. Only stored size is measured.
- Whether evidence fetching dominates latency. Simulated tool calls take no time.
- The LLM-judge comparison, until someone chooses to run and pay for it.
- Integrity. Logs can be edited after the fact and records cannot without detection; no arm here is
  given tampered data.

## Plugging in real agents

Agents run through `bench.handoff.sim.run_case`. The credit decision is behind the `DecisionAgent`
protocol (`RuleDecider`, `LayaDecider`). To test real agents, implement the same protocol, or replace
`_run_agent` with calls to your agents, keeping the three views: trace text, structured log lines and
the `Decision` calls that write records. Faults then come from the agents rather than from `faults_for`,
and ground truth needs labelling.

The judge is the `Judge` protocol in `arms.py`. `ClaudeJudge` builds its prompt from the same raw trace
arm 1 sees and parses a JSON answer.
