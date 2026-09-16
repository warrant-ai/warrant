# Gallery: lending

Two hundred synthetic personal-loan decisions made by `underwriter.py` under the `CR-07` policy in `examples/policies`, with outcomes attached to about half of them. Nothing here comes from a real customer.

Sets:

- `lending-all`: every decision.
- `lending-edge`: the decisions that later defaulted. The hard cases.

Targets:

- `underwriter-v2.3`: what was recorded. Replaying it should change nothing.
- `underwriter-v2.4`: the same decider with a cheaper model and the approval threshold raised from 720 to 750 through `params`.

Sets and targets live in `.warrant/` here, which is what `warrant` looks in by default. Run from this directory, with the `policy` extra installed:

```
warrant test lending-edge --against underwriter-v2.3 --fail-on flipped     # baseline: 0 flipped
warrant test lending-edge --against underwriter-v2.4 --fail-on flipped     # the change: see what flips
warrant test lending-all  --against underwriter-v2.4 --max-cost-increase 10%
```

Edit `THRESHOLD` or the model in `underwriter.py`, or the policy in `../../policies/CR-07.yaml`, and run again. Rebuild the sets with `python build.py`.
