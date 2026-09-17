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

`policy` is any object with a synchronous `evaluate(decisionClass, inputs)` that returns a `Verdict`:

```ts
import { Verdict, type PolicyEngine } from "warrantai";

const policy: PolicyEngine = {
  evaluate: (_class, inputs) =>
    Number(inputs.amount) <= 500000
      ? new Verdict("allow", { policyId: "CR-07", policyVersion: "2026.3", clause: "4.2" })
      : new Verdict("escalate", { policyId: "CR-07", policyVersion: "2026.3", clause: "4.3" }),
};
```

CEL policy bundles, the format the Python SDK evaluates, are not in this package yet.

## Local development

There is no local store in JavaScript. Run the collector from the Python package against a SQLite file and point the SDK at it:

```
pip install "warrantai[collector]"
warrant collector --store .warrant/records.db --token local:a-token-of-16-chars-or-more
WARRANT_STORE=http://127.0.0.1:8787 WARRANT_TOKEN=a-token-of-16-chars-or-more WARRANT_TENANT=local node agent.js
warrant export --store .warrant/records.db -o records.jsonl && warrant verify records.jsonl
```

## Also in the box

`validate(record)` and `loadSchema()` for the decision record schema v0, `currentDecision()` for integrations, and a small CLI: `warrant --version | schema | validate <file>`.

One cross-language note: content hashes of strings and bytes match the Python SDK exactly. JSON content matches too, except that JavaScript cannot tell `4.0` from `4`; hash a string when another language has to reproduce it.

Node.js 18 or newer. Types are included.

## Licence

Apache 2.0.
