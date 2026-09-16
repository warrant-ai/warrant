# warrantai

Warrant is the decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

This is release 0.0.1. It ships the decision record schema v0 and a validator so the format can be reviewed and integrated against before the SDK lands.

```
npm install -g warrantai
warrant --version
warrant schema
warrant validate examples/loan-approval.json
```

```js
import { validate, ValidationError } from "warrantai";

try {
  validate(record);
} catch (err) {
  if (err instanceof ValidationError) console.error(err.errors);
}
```

## What arrives next

- `decide()` context manager that opens a record, captures model and tool calls as evidence, and closes it on success or failure, off the hot path
- `check()` policy evaluation against a cached bundle before the action, with allow, deny or escalate
- local append-only store with hash chaining and an offline verifier
- `warrant test`: replay a set of real decisions against a changed target and fail the build on flipped decisions
- `warrant import`: reconstruct decision records from existing OpenTelemetry trace exports and run policy over them retrospectively

Roadmap and schema specification: https://warrantai.dev

## Licence

Apache 2.0.
