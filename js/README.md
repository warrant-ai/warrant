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
import { ActivityDecision, warrantActivityInterceptor } from "warrantai/adapters/temporal";

const guard = warrantActivityInterceptor(w, {
  disburse: new ActivityDecision({ decisionClass: "credit.disburse", subject: "loan_id", inputs: ["amount", "bureau_score", "foir"] }),
});
const worker = await Worker.create({ ..., activities, interceptors: { activity: [guard] } });
```

Before a mapped activity runs, its single object argument is checked against the policy (any other call shape is presented as `{ args }` for mapping functions to read). A denied or escalated activity is recorded as withheld and fails with a non-retryable `ApplicationFailure` of type `WarrantDenied` or `WarrantEscalated`, so the retry policy does not re-run it and the workflow can catch it and hand the case to a person; the failure's details carry the record id. Every record names the Temporal execution as evidence. One record per attempt, with ids derived from the attempt's identity, so a batch delivered twice is written once. The run's other activities are evidence for its next decision, by hash. `modelUsage(provider, model, { tokensIn, tokensOut })` inside an activity puts the model call's cost on the record. Workflow-side decisions and approval hand-offs are in the Python adapter today.

## Also in the box

`validate(record)` and `loadSchema()` for the decision record schema v0, `currentDecision()` for integrations, and a small CLI: `warrant --version | schema | validate <file>`.

One cross-language note: content hashes of strings and bytes match the Python SDK exactly. JSON content matches too, except that JavaScript cannot tell `4.0` from `4`; hash a string when another language has to reproduce it.

Node.js 18 or newer. Types are included.

## Licence

Apache 2.0.
