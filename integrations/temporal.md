# Warrant for Temporal

Warrant records every consequential action a Temporal workflow takes as a signed, verifiable decision record: which activity ran, under which policy clause, on what evidence, approved by whom, at what cost, and how it turned out. In Temporal's terms it is middleware: an interceptor on the worker that gates the activities you name as decisions, plus helpers for decisions made in workflow code and for handing an escalated activity to a person.

It changes nothing about how a workflow is written and holds no state of its own. The policy check is in-process; recording is asynchronous and never blocks an activity.

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

The plugin does three things: installs the interceptor, registers the `warrant.record` local activity that workflow-side helpers write through, and adds `warrant` to the workflow sandbox's passthrough modules so workflow code can import the helpers like any other module. Give the same plugin to `Replayer(plugins=[...])` so a replay sees the interceptor and sandbox the worker had. A worker that already lists the guard under `interceptors` is not given it twice.

Without the plugin: `interceptors=[guard]`, add `guard.record_activity` to `activities`, and import the helpers in workflow code under `workflow.unsafe.imports_passed_through()`.

### What happens on the activity side

`decisions` names the activity types that are decisions, keyed by activity type. Most activities are not; the rest are evidence. For a mapped activity:

1. Its arguments are read by parameter name, or field by field from a single dataclass argument. A call the mapping cannot read fails with a non-retryable `ApplicationError` of type `WarrantUnreadable` and is not a decision.
2. The inputs are checked against the policy bundle. `deny` and `escalate` record the attempt as withheld and raise a non-retryable `ApplicationError` of type `WarrantDenied` or `WarrantEscalated`, so the retry policy never re-runs a blocked activity. The error's details carry the record id, the policy, the clause and the reason.
3. `allow` and `unchecked` run the activity unchanged. Afterwards the decision is recorded as acted, with the run's other activity results since the last decision attached as evidence by hash, the model usage reported through `model_usage()` as cost, and the Temporal execution (namespace, workflow, run, activity, attempt) as a `temporal.execution` evidence item so an auditor can open the run.
4. An activity that raises is recorded as a failed decision. Its error text is never recorded; it can carry customer data.

One record per attempt. The record id is derived from the attempt's identity (`deterministic_ulid(current_attempt_scheduled_time, namespace|workflow|run|activity|attempt)`), so a retry is a new record and a batch delivered twice is written once.

### Decisions made in workflow code

```python
from warrant.adapters import temporal_workflow as warrant

@workflow.defn
class LoanApproval:
    @workflow.run
    async def run(self, loan):
        if workflow.patched("warrant-underwrite-decision"):
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

The activity side behaves as in Python: the single object argument is read (any other call shape is presented as `{ args }` for a mapping function), `WarrantDenied` and `WarrantEscalated` are non-retryable `ApplicationFailure`s with the record id in their details, failed attempts are recorded without error text, `modelUsage()` puts a model call's cost on the record, and record ids match the Python adapter byte for byte. Without the plugin, `warrantActivityInterceptor()` returns the factory for `interceptors: { activity: [...] }`. Workflow-side helpers for the TypeScript workflow bundle are not built yet.

## Test plan

Everything below runs in CI on every push and pull request, and on a weekly schedule so a new Temporal SDK release that breaks the adapter is caught without a code change.

**Integration tests, Python** (`python/tests/test_adapter_temporal.py`, workflow in `temporal_app.py` and `temporal_app_plugin.py`), on Temporal's time-skipping test server, Python 3.10, 3.12 and 3.13, latest `temporalio` at run time:

- an allowed activity runs and is recorded with evidence, identity and cost
- deny and escalate withhold the activity and fail it without retry
- an unreadable call is blocked and is not a decision
- a failed attempt is recorded without error text, and the retry is a new record
- record ids are deterministic, so a re-sent record is not written twice
- arguments are read by name or from a single dataclass
- a workflow-side decision is recorded through the local activity, and a replay of the finished history, against a build that adds a second decision behind `workflow.patched`, writes nothing
- a workflow-side deny is withheld and the workflow branches on it
- an escalation approved by a person runs the activity and links the verdict; a rejection or a two-day timeout records the verdict and runs nothing
- the plugin installs the interceptor, the record activity and the sandbox passthrough on a worker given none of them, and a replay through `Replayer(plugins=[...])` writes nothing
- the plugin does not double a guard also given as an interceptor

**Integration tests, TypeScript** (`js/test/adapter-temporal.test.js`, workflow bundled from `test/temporal-workflows.js`), Node 20 and 22, `@temporalio/*` 1.24: the same activity-side cases, record-id parity with Python pinned to a constant, and the plugin on a worker given no interceptors.

**Side effects on replay.** The only side effect the adapter has in workflow code is the record write, and it is a local activity whose result Temporal keeps in history. The replay tests above assert the record count after the replay. New call sites are behind `workflow.patched` in the test workflow for the same reason they must be in yours.

**Cross-language and record integrity**, every run: 20,000 random records sealed in Python and verified in JavaScript and the reverse; the conformance vectors for hashes, seals, signatures and Merkle proofs; the shared policy cases both CEL engines must evaluate identically.

**Versions this document was checked against:** `temporalio` 1.34.0 and `@temporalio/worker` 1.24.0, on 8 October 2026.

## Open

- Workflow-side helpers in TypeScript: [#6](https://github.com/warrant-ai/warrant/issues/6)
- Nexus operations as decisions, with the caller citing the handler's record: [#7](https://github.com/warrant-ai/warrant/issues/7)
- A lint for `decide()` call sites not behind `workflow.patched`: [#8](https://github.com/warrant-ai/warrant/issues/8)
- A worked example with a `temporalio.contrib` agent integration: [#10](https://github.com/warrant-ai/warrant/issues/10)

Two design choices we would like a Temporal engineer's view on: the lifetime of the evidence buffer (`EvidenceLog(max_sessions=)` bounds runs that end without a decision, and is our guess at how long a run can stay open), and approvals riding as one header set from a context variable rather than as a search attribute.
