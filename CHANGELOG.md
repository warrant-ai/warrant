# Changelog

## 0.1.0 (unreleased)

The v0.1 preview: everything a developer needs to record decisions locally, check them against policy, replay them against a change, and start from traces they already have.

- Python SDK: `Warrant` client and `decide()` context manager with `check()`, evidence by hash and reference, `model_call`, `tool_call`, `tool`, cost, `act()`, `require_human()`; `outcome()` and `human_verdict()` as linked records; client-side redaction; asynchronous batched emission with disk spill
- Local append-only SQLite store with per-stream hash chaining; `warrant export` and the offline `warrant verify`
- CEL policy bundles behind `check()` with per-policy fail modes, `warrant policy test` and `warrant policy check` (extra: `warrantai[policy]`)
- Replay: `warrant set create`, `warrant target add`, `warrant test` with frozen and live modes, flips, new denies, cost delta, flips by recorded outcome, gates, JSON and JUnit reports
- OpenTelemetry generative-AI span capture inside a decision (extra: `warrantai[otel]`)
- `warrant import`: reconstruct decision records from OpenTelemetry trace exports with a taxonomy and check them retrospectively
- Gallery: a synthetic lending set with outcomes, a decider and two targets under `examples/gallery/lending`
- Schema v0 additions, all optional: `decision.status`, `decision.inputs`, `evidence[].name`, `mandate.reason`, `mandate.flagged`
- Requires Python 3.10 or newer

## 0.0.1

- Decision record schema v0 and validators in Python and JavaScript, with a `warrant` CLI offering `schema` and `validate`
