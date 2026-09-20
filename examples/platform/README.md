# The platform: Temporal × a decision model × Warrant

Three layers, one alert, ninety days.

```
python run_platform.py
```

About ten seconds later you have a reliability curve over the band that was automated and an
evidence pack that verifies with nothing but the CLI. Everything is synthetic; no institution,
product or person here is real.

## What each layer supplies

| Layer | Supplies | Does not own |
|---|---|---|
| **Temporal** | The case lifecycle: retries, the human wait, and the durable ninety-day timer | What was decided, why, or whether it was right |
| **Decision model** | Typed answers with probabilities, in 70–500 ms | Process state, retries, or who is allowed to decide |
| **Warrant** | The immutable record, the mandate check, outcome linkage, calibration, the pack | Business logic, orchestration |

## What actually happens

1. The workflow starts `adjudicate_alert`. That activity calls the model through
   `DecisionAdapter.decide()`, which evaluates the questions, checks the mandate against `AML-01`
   and **writes the decision record in the same operation**. There is no path that calls the model
   without producing a record.
2. Because the call happens inside a Temporal activity, the execution lands on the record by
   itself — `temporal.execution` evidence, and a record id derived from the attempt, so a retry is
   a new record and a re-sent batch is not. The application passes nothing to make this happen.
3. If the policy routed the alert to a person, the workflow waits on the application's own signal,
   for hours or weeks. The reviewer's verdict becomes a linked record. Warrant records who it was
   told; it never authenticates them.
4. Either way the workflow then sets a timer at the ninety-day horizon. When it fires, it asks the
   system of record what happened and links the outcome — or, if there is still no answer, leaves
   the decision **outcome-pending** rather than guessing.

Step 4 is why Temporal is in the stack rather than a cron table. Nothing else survives a deploy, a
restart and a quarter to close the loop, and without that loop there is no calibration.

## The composition rule

`adjudicate_alert` is deliberately **not** in the Temporal adapter's `ToolDecision` mapping, and
the interceptor is built with `workflow_only=True`. The model adapter owns the record because it
holds the answers, the confidences and the state digest; naming the activity as well would record
the same decision twice. Activities that act without a model — `close_alert` — are what that
mapping is for.

## What it prints, and how to read it

```
outcome-attached share in stream 'aml': 97/120 (80.8%)

calibration for stream 'aml', answer 'disposition'
  over decisions where: decision.route == 'auto'
  correct when: outcome.label == 'stayed_closed'
  28 decision(s), 28 with confidence, 26 with an outcome (92.9% attached), 26 usable
  stated 0.953 vs observed 0.846   ECE 0.1069   MCE 0.1069   Brier 0.1411
  band          n   stated  observed   gap
  0.90-1.00     26   0.953     0.846  -0.107
```

The model said it was right 95% of the time in the band that was automated. It was right 85% of the
time. That gap is the whole product: a bank cannot see it from logs, from a trace vendor, or from
its case management system, because none of them hold both the stated confidence and what actually
happened.

**Read the coverage line first.** A curve over 93% of the automated band is a claim; the same curve
over 12% is not. The two numbers always travel together, in the terminal and in the pack.

Roughly a fifth of alerts have no answer even at the horizon, so they stay outcome-pending and are
excluded from the curve. That is deliberate: a demo where every decision resolves neatly teaches
the opposite of what this is for.

## With a real model

```
export TYPESAFE_API_KEY=...
export JEV_MODEL=jev-1.13.0          # a pinned version, never an alias
python run_platform.py
```

The run says which model answered. A scripted model produces a curve that proves nothing, and the
output says so rather than letting it be mistaken for one that does.

**Pin the version.** `jev-latest` moves when the vendor publishes, so a reliability curve measured
against it describes a model that may no longer exist. The adapter refuses an alias unless you pass
`allow_alias=True` and accept that calibration cannot be attributed to a version.

## Then

```
cd pack && warrant verify records.jsonl
```

That re-computes every hash and walks the chain, using nothing but the open-source CLI. The pack's
front page states what the chain proves — that these records have not changed since they were
sealed — and what it does not: that the operator could not have re-sealed the whole chain.
Per-writer signing and external anchoring are not built, and saying so is what makes the rest of
the document credible.

## Files

| File | What it is |
|---|---|
| `alert_workflow.py` | The workflow, its activities, and the outcome timer |
| `run_platform.py` | Worker, time-skipping server, the run, and the report |
| `../gallery/aml/policies/AML-01.yaml` | The policy, with segment carve-outs and its own tests |

`records.db` and `pack/` are generated; both are gitignored. Regenerate with `run_platform.py`.
