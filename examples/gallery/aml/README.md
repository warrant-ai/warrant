# AML alert adjudication gallery

500 synthetic transaction-monitoring alerts, adjudicated by a six-question set with a stated
confidence, checked against `AML-01`, with the 90-day outcome attached for the four in five where
the observation window has closed.

Everything here is synthetic. No institution, product or person is real, and nothing is derived
from real data. It exists so the whole path — record, check, outcome, calibrate, pack — runs on a
clean machine with no vendor, no API key and no network.

## Build it

```
python build.py
```

That writes `records.db` and `outcomes.csv` here (both gitignored; regenerate rather than commit).

## The finding it is built to show

The adjudicator is well calibrated below 0.90 and **overstates itself above it**. That is deliberate,
and it is the failure mode the product exists to catch: the high-confidence band a bank would
automate first is the one where stated confidence is furthest from the truth, and nothing but an
outcome-linked ledger shows it.

```
warrant calibrate --store records.db --stream aml \
  --where "decision.route == 'auto'" \
  --correct-when "outcome.label == 'stayed_closed'" \
  --answer disposition --by inputs.segment
```

```
  125 decision(s), 125 with confidence, 95 with an outcome (76.0% attached), 95 usable
  stated 0.954 vs observed 0.832   ECE 0.1224   MCE 0.1224   Brier 0.1524
  band          n   stated  observed   gap
  0.90-1.00     95   0.954     0.832  -0.122
  by inputs.segment:
    retail: n=56  stated 0.954  observed 0.804  ECE 0.1502
    sme: n=39  stated 0.954  observed 0.872  ECE 0.0826
```

Read it as: a quarter of alerts were auto-closed on a stated 95% confidence, and 17% of those came
back. The overall figure is bad enough; the segment split shows retail is worse than the average
and SME better, which is the reason a single number is never the answer.

`--where` matters. Calibration over every decision is a different question from calibration over
the band and segment about to be automated, and it is the second that decides whether automating
is safe.

## The evidence pack

```
warrant pack --store records.db --stream aml -o /tmp/aml-pack \
  --policy policies \
  --correct-when "outcome.label == 'stayed_closed'" \
  --where "decision.route == 'auto'" \
  --answer disposition --by inputs.segment
```

The pack holds the sealed records, the manifest, the coverage, the reliability curve, the policy
text that applied, and a front page that says what the chain proves and what it does not. Verify it
with nothing but the CLI:

```
cd /tmp/aml-pack && warrant verify records.jsonl
```

## The carve-outs are in the policy, not in the prose

`AML-01` never routes `pep` or `trade_finance` to AUTO at any confidence, and a structuring pattern
always goes to a person. The failure mode that matters in this decision type is not a missed fraud —
it is an alert closed by machine that should have become a suspicious transaction report, which is
personal liability for the money laundering reporting officer. `fail_mode: closed` follows from the
same reasoning: if a clause cannot be evaluated, a human sees the alert.

Those rules are covered by tests inside the policy file:

```
warrant policy test policies/
```

## Files

| File | What it is |
|---|---|
| `build.py` | Regenerates the store and the outcome file |
| `adjudicator.py` | The synthetic decider: six questions, one disposition, one confidence |
| `policies/AML-01.yaml` | Auto-closure policy with segment carve-outs and its own tests |

## What is not simulated

Decision timestamps are the moment `build.py` runs, so the 90-day horizon is what this set
*represents* rather than something faked into the dates — backdating observations against real
decision times would put an outcome before the decision it describes. Outcome coverage is capped
at four in five, because a set where every decision already has an outcome is not one a bank would
recognise.
