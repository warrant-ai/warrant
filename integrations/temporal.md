# Warrant for Temporal

Warrant records every consequential action a Temporal workflow takes as a signed, verifiable record: which activity ran, under which policy clause, on what evidence, approved by whom, at what cost, and how it turned out. It ships as a worker plugin. The plugin installs two interceptors: an activity inbound interceptor that checks the activities you name against a policy before they run and records them afterwards, and a workflow outbound interceptor that carries data to those activities in headers. It also ships helpers for recording a decision made in workflow code and for running an activity a person has approved.

A few Warrant terms, since some collide with Temporal's:

- A **decision** is a business action you choose to record, such as approving a loan or paying out. It has nothing to do with the workflow tasks that early Temporal called decision tasks. Most decisions are activities you name; workflow code can also record one with `decide()`.
- A **record** is Warrant's own signed entry, written to Warrant's store, not to workflow history. It is **sealed** when the store hashes it, chains it to the record before it and signs it; after that it cannot be changed.
- **Evidence** is what a record cites: the results of the run's earlier activities, by hash, and the workflow execution itself, so an auditor can open the run in the Temporal UI.
- A policy check returns **allow**, **deny** or **escalate**. Escalate means a person has to approve before the activity runs.

Workflow code does not change. The policy check runs in-process on the worker; recording is asynchronous and never blocks an activity.

| | Python | TypeScript |
|---|---|---|
| Package | `warrantai[temporal]` on PyPI, `temporalio >= 1.32`, Python 3.10+ | `warrantai` on npm with `@temporalio/activity ^1.24`, Node 20.3+ |
| Plugin | `warrant.adapters.temporal.WarrantPlugin`, named `warrant.WarrantPlugin` | `warrantPlugin()` from `warrantai/adapters/temporal`, named `warrantai.WarrantPlugin` |
| Activity side | gate, record, evidence, cost, approvals | gate, record, evidence, cost |
| Workflow side | `decide()`, `escalation()`, `approved()`, `rejected()`, `verdict()` | not yet, [issue #6](https://github.com/warrant-ai/warrant/issues/6) |
| Source | `python/src/warrant/adapters/temporal.py`, `temporal_workflow.py` | `js/src/adapters/temporal.js` |

Library documentation: [Python](../python/README.md), [JavaScript](../js/README.md). Record format: [spec/adr-0.2.md](../spec/adr-0.2.md).

## Python

```
pip install "warrantai[temporal,policy]"
```

```python
from temporalio.client import Client
from temporalio.worker import Worker
from warrant import Warrant, AgentInfo
from warrant.adapters import ToolDecision
from warrant.adapters.temporal import WarrantInterceptor, WarrantPlugin

w = Warrant(stream="lending", agent=AgentInfo("credit-underwriter", "2.3.1"), policy_bundle="policies", currency="INR")
guard = WarrantInterceptor(w, {
    "disburse": ToolDecision("credit.disburse", subject="loan_id", inputs=["amount", "bureau_score", "foir"]),
})
worker = Worker(client, task_queue="lending", workflows=[LoanApproval], activities=[underwrite, disburse],
                plugins=[WarrantPlugin(guard)])
```

Put the plugin on every worker: the ones that run the workflow and the ones that run its activities. The plugin does three things: installs the interceptors, registers the `warrant.record` local activity that workflow-side helpers write through, and adds `warrant` to the workflow sandbox's passthrough modules so workflow code can import the helpers like any other module. Give the same plugin to `Replayer(plugins=[...])` so a replay sees the interceptor and sandbox the worker had. A worker that already lists the guard under `interceptors` is not given it twice.

Without the plugin: `interceptors=[guard]`, add `guard.record_activity` to `activities`, and import the helpers in workflow code under `workflow.unsafe.imports_passed_through()`.

### What happens on the activity side

`decisions` names the activity types that are decisions, keyed by activity type. Most activities are not; the rest are evidence. For a mapped activity:

1. Its arguments are read by parameter name, or field by field from a single dataclass argument. A call the mapping cannot read fails with a non-retryable `ApplicationError` of type `WarrantUnreadable` and is not a decision.
2. The inputs are checked against the policy bundle. `deny` and `escalate` record the attempt as withheld and raise a non-retryable `ApplicationError` of type `WarrantDenied` or `WarrantEscalated`, so the retry policy never re-runs a blocked activity. The error's details carry the record id, the policy, the clause and the reason.
3. `allow` and `unchecked` run the activity unchanged. Afterwards the decision is recorded as acted, with the results of the run's earlier activities since the last decision attached as evidence by hash, the model usage reported through `model_usage()` as cost, and the Temporal execution (namespace, workflow, run, activity, attempt) as a `temporal.execution` evidence item so an auditor can open the run.
4. An activity that raises is recorded as a failed decision. Its error text is never recorded; it can carry customer data.

How the evidence reaches the decision: the workflow outbound interceptor sees each activity's result in workflow code, keeps its hash in workflow state, and attaches the hashes collected since the last decision to the decision activity's start as a `warrant-evidence` header. Nothing is held in worker memory, so the evidence reaches the decision on whichever worker runs it, and a replay on any worker rebuilds the same state. A decision that runs or is withheld uses up the evidence; one that fails outright leaves it for the next decision. Each item names the activity type and its activity id; when the workflow does not set an id, that is the SDK's default, the activity's sequence number in the run. If the worker running the workflow has no Warrant interceptor, there is no header, and the decision records no evidence rather than an incomplete list.

One record per attempt. The record id is derived from the attempt's identity (`deterministic_ulid(current_attempt_scheduled_time, namespace|workflow|run|activity|attempt)`), so a retry is a new record and a batch delivered twice is written once.

### Decisions made in workflow code

```python
from warrant.adapters import temporal_workflow as warrant

@workflow.defn
class LoanApproval:
    @workflow.run
    async def run(self, loan):
        verdict = await warrant.decide("credit.approve", subject=loan.id, inputs={...}, action="approve", summary=reason)
        try:
            return await workflow.execute_activity(disburse, loan, start_to_close_timeout=...)
        except ActivityError as exc:
            escalated = warrant.escalation(exc)
            if escalated is None:
                raise
            await workflow.wait_condition(lambda: self.review is not None, timeout=timedelta(days=2))
            if self.review.approve:
                return await warrant.approved(disburse, loan, reviewer=self.review.reviewer, record_id=escalated["record_id"], start_to_close_timeout=...)
            await warrant.rejected(escalated["record_id"], reviewer=self.review.reviewer, note=self.review.reason)
```

`decide()` writes through the `warrant.record` local activity, so the record and the verdict are in history and a replay never writes twice. Its ids come from `workflow.now()` and `workflow.uuid4()`, both deterministic. The local activity carries a summary (`warrant: <class> for <subject>`) for the Temporal UI.

**Adding `decide()` to a workflow that already has open executions.** `decide()` schedules a local activity, so adding a call to an existing workflow type changes the commands it produces, and open executions would fail replay with a non-determinism error. Treat it like any other workflow change: put the new call behind `workflow.patched(...)` or use Worker Versioning. A workflow written with `decide()` from the start needs neither. The activity-side interceptor schedules no commands, so adding the plugin to a worker needs no versioning at all.

Approvals use context propagation: `approved()` sets the approval on a context variable, the workflow outbound interceptor turns it into one `warrant-approval` header on the activity start, and the activity interceptor writes the reviewer's verdict as its own sealed record, linked to the escalated decision, before the activity runs. The reviewer's name comes from the application's own Update or Signal; Warrant records who it was told and never authenticates them. A denial is not an escalation: nobody can approve it.

### Composing with a decision model inside an activity

When an activity calls a decision model through `warrant.adapters.model.DecisionAdapter`, that adapter owns the record because it holds the answers, the confidences and the state digest. Do not also name the activity in `decisions`, or the same decision is recorded twice; build the interceptor with `WarrantInterceptor(w, {}, workflow_only=True)` so it exists only for the workflow-side helpers. `examples/platform` is the worked example: Temporal, a decision model and Warrant composed, with a durable ninety-day timer that links the outcome.

## TypeScript

```
npm install warrantai @temporalio/activity
```

```js
import { Worker } from "@temporalio/worker";
import { Warrant } from "warrantai";
import { ActivityDecision, warrantPlugin } from "warrantai/adapters/temporal";

const plugin = warrantPlugin(w, {
  disburse: new ActivityDecision({ decisionClass: "credit.disburse", subject: "loan_id", inputs: ["amount", "bureau_score", "foir"] }),
});
const worker = await Worker.create({ connection, taskQueue: "lending", workflowsPath, activities, plugins: [plugin] });
```

The activity side behaves as in Python, with one difference that matters: the TypeScript adapter has no workflow interceptor yet, so it still keeps a run's evidence in worker memory, and a decision misses the results of activities that ran on another worker. Until that is fixed ([#18](https://github.com/warrant-ai/warrant/issues/18)), evidence in TypeScript is complete only when a run's activities share a worker. Otherwise the single object argument is read (any other call shape is presented as `{ args }` for a mapping function), `WarrantDenied` and `WarrantEscalated` are non-retryable `ApplicationFailure`s with the record id in their details, failed attempts are recorded without error text, `modelUsage()` puts a model call's cost on the record, and record ids match the Python adapter byte for byte. Without the plugin, `warrantActivityInterceptor()` returns the factory for `interceptors: { activity: [...] }`. Workflow-side helpers for the TypeScript workflow bundle are not built yet.

## Test plan

Everything below runs in CI on every push and pull request, and on a weekly schedule so a new Temporal SDK release that breaks the adapter is caught without a code change.

**Integration tests, Python** (`python/tests/test_adapter_temporal.py`, workflow in `temporal_app.py` and `temporal_app_plugin.py`), on Temporal's time-skipping test server, Python 3.10, 3.12 and 3.13, latest `temporalio` at run time:

- an allowed activity runs and is recorded with evidence, identity and cost
- deny and escalate withhold the activity and fail it without retry
- an unreadable call is blocked and is not a decision
- a failed attempt is recorded without error text, and the retry is a new record
- record ids are deterministic, so a re-sent record is not written twice
- arguments are read by name or from a single dataclass
- the evidence reaches a decision that runs on a different worker from the activities it cites: three workers, each with its own interceptor, as separate processes would have; the run then replays cleanly
- with no interceptor on the worker that runs the workflow, a decision records no evidence
- a decision that fails on every attempt leaves the evidence for the next decision
- a workflow-side decision is recorded through the local activity, and a replay of the finished history writes nothing. The replay runs against a later build of the workflow that adds a second `decide()` behind `workflow.patched`, which is the upgrade case described above
- a workflow-side deny is withheld and the workflow branches on it
- an escalation approved by a person runs the activity and links the verdict; a rejection or a two-day timeout records the verdict and runs nothing
- the plugin installs the interceptor, the record activity and the sandbox passthrough on a worker given none of them, and a replay through `Replayer(plugins=[...])` writes nothing
- the plugin does not double a guard also given as an interceptor

**Integration tests, TypeScript** (`js/test/adapter-temporal.test.js`, workflow bundled from `test/temporal-workflows.js`), Node 20 and 22, `@temporalio/*` 1.24: the same activity-side cases, record-id parity with Python pinned to a constant, and the plugin on a worker given no interceptors.

**Side effects on replay.** The only side effect the adapter has in workflow code is the record write, and it is a local activity whose result Temporal keeps in history. The evidence hashes are computed from activity results already in history. The replay tests above assert the record count after the replay.

**Cross-language and record integrity**, every run: 20,000 random records sealed in Python and verified in JavaScript and the reverse; the conformance vectors for hashes, seals, signatures and Merkle proofs; the shared policy cases both CEL engines must evaluate identically.

**Versions this document was checked against:** `temporalio` 1.34.0 and `@temporalio/worker` 1.24.0, on 12 October 2026.

## Open

- Workflow-side helpers in TypeScript: [#6](https://github.com/warrant-ai/warrant/issues/6)
- Nexus operations as decisions, with the caller citing the handler's record: [#7](https://github.com/warrant-ai/warrant/issues/7)
- Evidence across workers in TypeScript, through a workflow interceptor: [#18](https://github.com/warrant-ai/warrant/issues/18)
- A worked example with a `temporalio.contrib` agent integration: [#10](https://github.com/warrant-ai/warrant/issues/10)

Questions we would like a Temporal engineer's view on: whether hashing activity results inside a workflow outbound interceptor is sound, given the result has been decoded by the payload converter by then; and whether there is a supported way for an outbound interceptor to read the activity id the SDK assigns, rather than mirroring its sequence number.
