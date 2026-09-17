# Changelog

## 0.2.0 (unreleased)

- Collector: a stateless HTTP service that seals record batches into a store, with per-tenant bearer tokens, health, readiness and Prometheus metrics (`warrant collector`; extra `warrantai[collector]`)
- PostgreSQL store for self-hosted deployments, chaining per tenant and stream under a row lock so many collectors can write concurrently
- SDK records to a collector URL with `Warrant(store="https://...", token=...)`; batches are gzip-compressed and spill to disk while the collector is unreachable
- Chains are now per tenant and stream; local SQLite stores from 0.1.0 migrate on first open; the verifier reports chains as `tenant/stream`
- `deploy/` with a Dockerfile and a docker-compose file
- JavaScript and TypeScript SDK: `Warrant`, `decide()`, evidence, cost, outcomes, human verdicts, redaction and background delivery to a collector with disk spill; type declarations included. The local store and replay remain Python-only
- Policy bundles in JavaScript (`warrantai/policy`, optional `@marcbachmann/cel-js` and `yaml`): the same files, fail modes and embedded tests as Python
- `conformance/policy-cases.json`: CEL cases both SDKs must evaluate identically, plus the known engine differences, pinned
- Policy linter in both SDKs: warns at load when a clause compares an input with a decimal literal or divides two inputs without `double()`. A whole-number input (`foir: 0`) compared with `0.45` cannot be evaluated by cel-python, so the policy's fail mode applied; the example policy CR-07 now uses `double(foir)`
- Requires Python 3.10 or newer

## 0.1.0 (2026-09-17)

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
