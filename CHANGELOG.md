# Changelog

## Unreleased

- Question sets as versioned artefacts (`warrant.questions`, `warrant questions lint|diff|show`). A registry is a directory of YAML or JSON files, several versions of a set side by side, and the version is already stamped on every record by `decision.question_set`. `lint` walks each consecutive pair, classifies what moved and **fails when the version bump was too small for the change** — a question removed, a primitive changed or permitted answers narrowed is breaking; editing the instructions is semantic, because the model is being asked a different thing and the answers stop being comparable even though nothing breaks structurally
- `DecisionAdapter(..., registry=...)` makes the pin enforceable: an unregistered or unpinned set cannot run, and the model's answers are checked against the questions that were asked, so an answer outside the permitted values is caught where it happened instead of surfacing as a distortion in a reliability curve
- `warrant pack --questions DIR` carries the sets the records actually cite into the pack, resolved before anything is written; a cited version the registry no longer holds stops the pack rather than producing one whose decisions cannot be interpreted
- The AML gallery gains `question-sets/aml.alert@3.1.0.yaml`, and the platform example loads it — with a real key, the questions Jev is asked are built from the registry, so the text asked and the text stamped on the record are the same by construction


## 0.4.0 (2026-09-20)

The other half of the ledger. A decision record without an outcome is a log: this release attaches what actually happened, measures whether the stated confidence held on your own decisions, and assembles the result into something a model validation committee will read. Plus the boundary a decision model crosses, with TypeSafe's Jev as its first implementation, and the three layers composed into a worked example that runs on one machine.


- The three layers composed, with a worked example in `examples/platform`: a Temporal workflow that adjudicates an alert through a decision model, waits on a human where the policy says so, and sets a durable ninety-day timer that wakes to link what actually happened. `python run_platform.py` runs the whole thing on one machine against Temporal's time-skipping server, so the horizon really elapses, and prints the reliability curve and an evidence pack
- `DecisionAdapter` (`warrant.adapters.model`) is vendor-neutral and picks up Temporal by itself: inside an activity the execution lands on the record as `temporal.execution` evidence and the record id derives from the attempt, so a retry is a new record and a re-sent batch is not. The activity that makes a model decision must not also be named in the Temporal adapter's `ToolDecision` mapping, or the decision is recorded twice; `WarrantInterceptor(..., workflow_only=True)` is the shape for that case. `warrant.adapters.jev` keeps `JevModel` and re-exports the adapter
- `temporal_workflow.verdict(record_id, reviewer=, verdict=, note=)`: record a person's approve, reject or amend against a decision already written, as a linked record. `rejected()` is now the reject case of it
- Jev adapter (`warrant.adapters.jev`, extra `warrantai[jev]`, `typesafe-sdk>=0.7`) and the decision-model boundary it sits behind (`warrant.adapters.base.DecisionModel`). One call evaluates typed questions, checks the mandate and writes the record together, so there is no path that calls the model without producing one. Jev's three primitives are normalised before anything is written — a Choice keeps its `probabilities`, a Score keeps its rubric distribution, and a Noul becomes a value, a confidence and the two-outcome distribution it always was — so no vendor vocabulary reaches the schema and a fake model is interchangeable with the real one outside `adapters/`
- The adapter **requires a pinned model version**. TypeSafe's default is `jev-latest`, an alias that moves when they publish, and a reliability curve measured against an alias describes a model that may no longer exist. An alias raises unless `allow_alias=True`
- Where inference happened is recorded on every record as a `model.endpoint` evidence item with its region, and a decision class can declare a required region: the adapter refuses to send rather than discovering the problem in an audit. Regions it cannot positively identify are reported as `unknown`, never guessed
- `persist` and `on_ledger_unavailable` are per decision class, defaulting to the SDK's asynchronous, never-blocking behaviour; the mode in force rides on every record as a `warrant.persistence` evidence item
- `Decision.state(digest=, ref=)`: the writer for the `state_digest` and `state_ref` schema fields — hash the state, keep the snapshot separately
- `warrant pack`: assemble an evidence pack — the sealed records, a manifest with the chain head and the policy and question-set versions in force, outcome coverage, an optional reliability curve, the policy text that applied, and a front page written for the committee that reads it. The chain is verified before anything reaches disk, so a pack that fails its own verification is never written, and a non-empty output directory is refused. The front page states plainly what a hash chain proves and what it does not, because per-writer signing and external anchoring are not built. Apache-licensed on purpose: an evidence store a customer cannot extract without us is one they cannot rely on
- `Decision.answer()`, `Decision.question_set()` and `act(route=...)`: the SDK surface that writes the schema v0 additions below — typed answers with the full distribution, the registered question-set version, and where the policy sent the decision
- `warrant calibrate --where`: narrow the curve to the decisions that matter, for example the band and segment about to be automated. Calibration over every decision is a different question from calibration over what you are about to automate
- New AML gallery (`examples/gallery/aml`): 500 synthetic alerts, a six-question set with stated confidences, segment carve-outs that never auto-close, and an adjudicator that is deliberately overconfident above 0.90 — the failure mode an outcome-linked ledger exists to catch
- `warrant outcomes ingest`: attach realised outcomes to decisions already recorded, from a CSV keyed by subject or record id, with `label` and optional `observed_at`, `score` and `source`. Each row becomes a linked `outcome` record; ids derive from the decision, the label and the observation time, so re-sending a file writes nothing new and a corrected label lands as a later record with both surviving. Rows matching no decision are reported, not raised. Works the same on decisions reconstructed by `warrant import`, so history whose outcomes are already known can be joined in one pass
- `warrant outcomes status`: the outcome-attached share, overall and per decision class — the depth metric behind every calibration claim
- `warrant calibrate`: Expected Calibration Error, a reliability curve, MCE and Brier score over the decisions that carry both a stated confidence (`decision.answers[]`) and an outcome. What counts as correct is stated as a CEL expression over the joined record rather than inferred; `--by` breaks the curve down by `class`, `question_set`, `route` or `inputs.<field>`; `--max-ece` and `--max-mce` gate it in CI. Coverage is always printed beside the number, because a curve over 12% of decisions is a different statement from one over 90%
- Schema v0 additions, all optional: `decision.question_set` (registered id and semver version), `decision.state_digest` and `decision.state_ref` (hash the state, keep the snapshot separately), `decision.answers[]` (typed answers with the full distribution, not the winning value) and `decision.route` (auto, human, model, deferred). Records written before them keep validating, and the fields seal, export and verify like any other part of the body

## 0.3.0 (2026-09-19)

Agents you do not wrap by hand: framework adapters for the Claude Agent SDK, LangGraph and Temporal, an MCP server for hosts that speak the protocol, and a GitHub Action that replays real decisions in CI.

- Framework adapters (`warrant.adapters`): name the tools that are decisions with `ToolDecision`, and the adapter gates them on the policy, records acted, withheld and failed decisions, and attaches the session's other tool results as evidence. Claude Agent SDK hooks (`warrantai[claude-agent]`) and a LangGraph `ToolNode` guard with `interrupt()` for escalations (`warrantai[langgraph]`), and a Temporal adapter in both SDKs (`warrantai[temporal]`, `warrantai/adapters/temporal`) that gates and records the activities named as decisions with no workflow-code change, one record per attempt with ids derived from the attempt's Temporal identity. In Python, workflow code can also record its own decisions through a local activity and hand a reviewer's approval back to an escalated activity (`warrant.adapters.temporal_workflow`)
- `Warrant.decide(record_id=...)` and `human_verdict(record_id=...)` accept a deterministic ULID for callers whose runtime may deliver the same event twice; `deterministic_ulid` now lives in `warrant.ids`, and the JS SDK has `deterministicUlid` with the same bytes plus `recordId` on `decide()` and `humanVerdict()`
- GitHub Action (`action/`): replay a decision set against a target in CI, fail the check on the gates, and write a job summary that names each changed decision with its recorded outcome
- MCP server (`warrant mcp`, extra `warrantai[mcp]`): `describe_mandate`, `check_mandate`, `record_decision` and `record_outcome` over stdio for MCP-capable agents. The agent's identity comes from the server's configuration and the mandate is always evaluated by the server, never supplied by the agent

## 0.2.0 (2026-09-17)

Shared deployments and a second language: a collector and a PostgreSQL store, the JavaScript and TypeScript SDK, and policy bundles that evaluate the same way in both.

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
