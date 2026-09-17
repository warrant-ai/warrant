# Warrant replay action

Replays a saved set of real decisions against the change in a pull request and fails the check when decisions flip, the policy newly denies, or cost rises past your limit. The job summary lists the exact decisions affected, with what happened to each one in production.

```yaml
- uses: actions/checkout@v4
- uses: warrant-ai/warrant/action@v0.3.0
  with:
    set: lending-edge
    against: underwriter-v2.4
    max-cost-increase: 10%
```

The workspace (`.warrant/sets/*.jsonl` and `.warrant/targets.json`) and the decider module live in your repository; see "Replay" in the Python README for how to build them. Nothing leaves the runner.

| Input | Default | |
|---|---|---|
| `set` | required | Decision set to replay |
| `against` | required | Target: an agent version, model, policy and decider |
| `fail-on` | `flipped,new-deny,unreplayable,errored` | Gates that fail the check |
| `max-cost-increase` | none | For example `10%` |
| `mode` | `frozen` | `frozen` serves recorded tool results, so only your change runs again |
| `working-directory` | `.` | Where the workspace and decider are |
| `workspace` | `.warrant` | Relative to `working-directory` |
| `install` | `warrantai[policy]` | Pin it, for example `warrantai[policy]==0.3.0` |
| `python-version` | `3.12` | |

Outputs: `passed`, `flipped`, and `report` (path of the JSON report; a JUnit file is written next to it as `warrant-replay.xml`).

A decider that raises always fails the run, whatever the gates say.
