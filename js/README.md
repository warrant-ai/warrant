# warrantai

Warrant is the decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

This package is the JavaScript and TypeScript SDK. It records decisions to a Warrant collector. The local store, policy bundles, replay and import live in the [Python package](https://pypi.org/project/warrantai/); both write the same record format, and the collector seals records from either into the same chain.

```
npm install warrantai
```

```ts
import { Warrant } from "warrantai";

const w = new Warrant("lending", {
  store: "https://collector.internal",          // or $WARRANT_STORE
  token: process.env.WARRANT_TOKEN,
  agent: { name: "credit-underwriter", version: "2.4.0" },
  currency: "INR",
});

let decisionRecordId;
const approved = await w.decide("credit.approve", { subject: loan.id }, async (d) => {
  decisionRecordId = d.recordId;
  const verdict = d.check({ amount: loan.amount, bureau_score: bureau.score });
  d.evidence("bureau_pull", { uri: bureau.uri, content: bureau.raw });        // hashed here, never stored
  d.modelCall("anthropic", "claude-sonnet-5", { tokensIn: 1200, tokensOut: 300, amount: 3.5 });
  if (!verdict.allowed) return false;                                         // recorded as withheld
  await disburse(loan);
  d.act("approve", { summary: "Within limit.", costCentre: "retail-lending" });
  return true;
});

w.outcome({ label: "performing", decisionRecordId });                          // months later
await w.close();                                                               // on shutdown
```

`decide()` runs your function as one decision scope and records it when the function settles: `acted` if `act()` was called, `withheld` if it was not, `failed` if it threw. A failure records the error's class name only, never its message, and the error is rethrown. `decide()` resolves to whatever your function returns.

## What it guarantees

- **Never on your agent's path.** Records are queued and delivered in batches in the background. While the collector is unreachable they spill to `.warrant/spill` and are delivered when it returns; a batch the collector rejects outright is moved to `.warrant/spill/dead` instead of retried. `stats()` reports all of it.
- **Evidence by reference.** `evidence()` hashes content in-process and records the hash and a URI. The content itself never leaves.
- **`unchecked` is never allowed.** Without a policy engine `check()` returns `unchecked`, and `verdict.allowed` is true for `allow` only.
- **Redaction before the wire.** `new Redactor({ patterns, fields })` rewrites free-text fields and drops named fields; patterns never touch ids, hashes or URIs.
- **Unsealed in flight, sealed at rest.** The SDK never seals. The collector's store assigns the sequence and the hash chain, and skips record ids it has already stored, so retries are safe.

## Policy checks

The same policy bundles the Python SDK evaluates: YAML or JSON files of CEL clauses, with fail modes and embedded tests.

```
npm install @marcbachmann/cel-js yaml        # optional packages; the CEL engine needs Node.js 20.19+
```

```ts
import { Warrant } from "warrantai";
import { CelPolicyEngine, PolicyBundle, runPolicyTests } from "warrantai/policy";

const bundle = await PolicyBundle.load("policies/");
const failures = runPolicyTests(bundle).filter((t) => !t.passed);     // the tests embedded in each file
const w = new Warrant("lending", { store, policy: new CelPolicyEngine(bundle) });
```

`check()` stays synchronous and in-process. The first clause whose `when` is true decides; if none matches, the policy's `default` applies; if a clause cannot be evaluated (a missing input, a type error), the policy's `fail_mode` applies and the result is flagged for review.

Both SDKs run the shared cases in `conformance/policy-cases.json`, so a bundle behaves the same whichever language your agent is written in. Two patterns are not portable, and the loader warns about them:

- **Compare an input with a decimal through `double()`**: write `double(foir) <= 0.45`, not `foir <= 0.45`. A whole-number input such as `0` or `1` is an int, and CEL engines disagree on comparing an int with a double.
- **Divide through `double()`**: write `double(emi) / double(income)`. Whole numbers divide as integers, so `30000 / 50000` is `0`.

Any object with a synchronous `evaluate(decisionClass, inputs)` that returns a `Verdict` also works as `policy`, if your rules live somewhere else.

A policy file may carry `effective_from` and `effective_to` (a date, read as midnight UTC, or an RFC 3339 timestamp; one with an offset such as `+05:30` is converted to UTC, and one without an offset is UTC). Two versions of one policy can then sit in the same bundle, and `check()` applies the one in force when the decision was opened; `evaluate` receives that moment as a third argument. Windows that overlap are refused at load.

## Local development

There is no local store in JavaScript. Run the collector from the Python package against a SQLite file and point the SDK at it:

```
pip install "warrantai[collector]"
warrant collector --store .warrant/records.db --token local:a-token-of-16-chars-or-more
WARRANT_STORE=http://127.0.0.1:8787 WARRANT_TOKEN=a-token-of-16-chars-or-more WARRANT_TENANT=local node agent.js
warrant export --store .warrant/records.db -o records.jsonl && warrant verify records.jsonl
```

## Temporal

If your agent runs on Temporal, the activities you name as decisions are gated and recorded with no change to workflow code (needs `@temporalio/activity`):

```js
import { Worker } from "@temporalio/worker";
import { ActivityDecision, warrantPlugin } from "warrantai/adapters/temporal";

const plugin = warrantPlugin(w, {
  disburse: new ActivityDecision({ decisionClass: "credit.disburse", subject: "loan_id", inputs: ["amount", "bureau_score", "foir"] }),
});
const worker = await Worker.create({ ..., activities, plugins: [plugin] });
```

The plugin adds the activity interceptor and a workflow interceptor module to the worker and appears in its logs as `warrantai.WarrantPlugin`; put it on every worker, the workflow's and the activities'. Without it, use `interceptors: { activity: [warrantActivityInterceptor(w, decisions, options)], workflowModules: [warrantWorkflowInterceptorModule(decisions)] }` and register `warrantRecordActivity(w)` under `RECORD_ACTIVITY` (`"warrant.record"`) in `activities`. Needs `@temporalio/activity` and `@temporalio/workflow`. The full guide, with the test plan, is `integrations/temporal.md`.

Before a mapped activity runs, its single object argument is checked against the policy (any other call shape is presented as `{ args }` for mapping functions to read). A denied or escalated activity is recorded as withheld and fails with a non-retryable `ApplicationFailure` of type `WarrantDenied` or `WarrantEscalated`, so the retry policy does not re-run it and the workflow can catch it and hand the case to a person; the failure's details carry the record id. Every record names the Temporal execution as evidence. One record per attempt, with ids derived from the attempt's identity, so a batch delivered twice is written once. The run's earlier activities are evidence for its next decision, by hash: the workflow interceptor keeps the hashes in workflow state and passes them to the decision in a header, so they reach it on any worker. `modelUsage(provider, model, { tokensIn, tokensOut })` inside an activity puts the model call's cost on the record.

### Decisions and approvals in workflow code

`warrantai/adapters/temporal/workflow` is imported in workflow code. `decide()` records a decision the workflow makes itself; `escalation()`, `approved()` and `rejected()` handle an activity the policy escalated to a person:

```js
import { condition, defineUpdate, proxyActivities, setHandler } from "@temporalio/workflow";
import { approved, decide, escalation, rejected } from "warrantai/adapters/temporal/workflow";

const ACTIVITY = { startToCloseTimeout: "1 minute" };
const { disburse } = proxyActivities(ACTIVITY);
export const review = defineUpdate("review");

// A decision made in workflow code: recorded, and its verdict returned to branch on.
export async function creditCheck(loan) {
  const inputs = { amount: loan.amount, bureau_score: loan.bureau_score, foir: loan.foir };
  return decide("credit.approve", { subject: loan.id, inputs, action: "approve" });
}

// An activity the policy escalates: wait for a reviewer's Update, then run it approved, or record the rejection.
export async function loanApproval(loan) {
  let decision;
  setHandler(review, (d) => { decision = d; });
  try {
    return await disburse(loan);
  } catch (err) {
    const escalated = escalation(err);
    if (escalated === null) throw err;
    if (!(await condition(() => decision !== undefined, "2 days"))) {
      return rejected(escalated.record_id, { reviewer: "system", note: "timed out" });
    }
    if (!decision.approve) return rejected(escalated.record_id, { reviewer: decision.reviewer, note: decision.reason });
    return approved("disburse", [loan], { reviewer: decision.reviewer, recordId: escalated.record_id, ...ACTIVITY });
  }
}
```

- `decide(decisionClass, { subject, inputs, action, summary, alternatives, onBehalfOf })` checks the policy and records the decision through the `warrant.record` local activity, so the record is in history and a replay writes nothing. It resolves to `{ record_id, status, result, policy_id, policy_version, clause, reason, flagged }`: `acted` for allow or unchecked, `withheld` for deny or escalate. Ids come from the workflow's start time and `uuid4()`, so a retried workflow task writes the same record again and the store keeps one.
- `escalation(err)` returns the details of a `WarrantEscalated` failure, or `null`; `blocked(err)` also returns a denial's. A denial cannot be approved.
- `approved(activityType, args, { reviewer, recordId, note, ...activityOptions })` schedules the activity with a `warrant-approval` header. The activity side writes the reviewer's verdict as its own record, linked to the escalated decision, before the activity runs, and the activity's record names the reviewer. The reviewer's name comes from your own Update or Signal; Warrant records who it was told and does not authenticate them. If the workflow's worker has no Warrant interceptor module, `approved()` throws instead of running the activity without the approval.
- `rejected(recordId, { reviewer, note })` and `verdict(recordId, { reviewer, verdict, note })` record a person's verdict, or a timeout, as a linked record.

The header and request shapes are the Python adapter's, so a JS workflow can approve an activity that a Python worker runs; a test runs exactly that. `decide()` schedules a local activity, so adding a call to a workflow type that already has open executions needs `patched()` or Worker Versioning; a workflow written with it from the start needs neither.

## Agent Decision Record

Records carry the fields of the Agent Decision Record 0.2 (`spec/adr-0.2.md` in the repository), and from 0.7.1 the JavaScript SDK also applies its evidence rules: obligations, the warrant, fail-closed commit and lifecycle transitions.

```js
await w.decide("credit.approve", { subject: "LN-7731" }, async (d) => {
  d.evidence("pan", { uri: "kyc://req/1", type: "document", provider: "nsdl", content: pan, sensitive: true });
  d.cite(partnerRecord, { keyring: Keyring.fromKeySets(partnerKeySet), obligation: "OB-2" });
  d.claim("gst_turnover_inr", 42000000);
  d.retention("rbi-credit-8y", { retainUntil: "2034-09-27T00:00:00Z" });
  d.act("approve");
});
```

- **Sensitive evidence** gets a salted digest, and the salt travels to the collector's sidecar, never onto the record. `saltFor(digest)` returns it. A sensitive item cannot carry an excerpt.
- **`cite()`** relies on an upstream sealed record by id and hash. It refuses a record that no longer matches its seal and, given a keyring, one whose issuer signature does not verify. The parent's claims are not copied.
- **Agent identity** takes `model`, `runtime` and `identity: [registry, id]`.
- **Verifier primitives:** `verifySeal`, `Keyring`, `SigningKey`, `recordHash`, `saltedHash`, and the RFC 9162 Merkle functions. They produce the same bytes as the Python package; `conformance/adr-vectors.json` is checked by both test suites.

### Obligations and the warrant

A policy bundle says what has to be true, from which providers and how fresh, and whether acting without a warrant must fail closed:

```yaml
enforce: true
retention: {class: rbi-credit-8y, period: 2922d}
obligations:
  - {id: OB-1, requires: tool_call, name: bureau_pull, providers: [cibil, experian], max_age: 30d}
  - {id: OB-2, requires: record, providers: [partner-data], max_age: 7d}
  - {id: OB-3, requires: human_review, when: amount > 2500000}
```

```js
await w.decide("credit.msme.approve", { subject: "LN-7731" }, (d) => {
  d.check({ amount: 2_000_000, bureau_score: 742 });                 // picks up the obligations that apply
  d.evidence("bureau_pull", { uri: "cibil://req/55120", type: "tool_call", provider: "cibil", content: bureau, retrievedAt });
  d.cite(partnerRecord, { keyring: partnerKeys });
  const state = d.warrant();                                          // { state, met, unmet, rejected, warranted }
  d.commit("approve");                                                // throws NotWarranted without a warrant
});
```

The seven admissibility rules, the lifecycle and the transition checks are a line-for-line port of the Python SDK's; `conformance/admissibility-cases.json` is generated from Python and both test suites must reproduce it exactly, reason codes and messages included. `new Warrant(stream, { enforce: true })` makes `act()` fail closed everywhere, not only where a policy says so.

**One difference: there is no local store.** `w.transition(decisionRecordId, toState, { decidedBy, fromState, decision, reviewer, shown })` needs the state being left (`fromState`), and, for leaving `escalated` or `pending_evidence` for `warranted`, the decision record itself (`decision`) so it can check that a named reviewer is linked to what they were shown and that no evidence is missing. A transition can supply a person, never evidence.

Python only: checkpoints and witnesses, `warrant verify` levels, `warrant trace`, packs, replay and import.

## Also in the box

`validate(record)` and `loadSchema()` for the decision record schema v0, `currentDecision()` for integrations, and a small CLI: `warrant --version | schema | validate <file>`.

Content hashes and seal hashes match the Python SDK exactly: both write canonical JSON as RFC 8785 specifies, so a record sealed by a Python store verifies here and the reverse. The exception is history: a record a Python store sealed before 0.9.0 (no `seal.canon`) that holds a number Python formats differently, such as `4.0` or `1e-07`, can only be re-hashed by the Python verifier.

Node.js 18 or newer. Types are included.

## Licence

Apache 2.0.
