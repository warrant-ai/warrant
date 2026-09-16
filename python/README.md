# warrantai

Warrant is the decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

This is the 0.1.0 development line. It ships the decision record schema, the `decide()` SDK with a local append-only store, and the offline verifier.

```
pip install warrantai
```

```python
from warrant import Warrant, AgentInfo, Redactor

w = Warrant(
    stream="lending",
    agent=AgentInfo("credit-underwriter", "2.3.1"),
    currency="INR",
    redact=Redactor(patterns=[r"\b\d{4}\s\d{4}\s\d{4}\b"]),
)

with w.decide("credit.approve", subject="LN-20431", on_behalf_of="branch:jayanagar") as d:
    verdict = d.check(amount=450000, bureau_score=748, foir=0.38)
    if not verdict.allowed:
        route_to_officer(verdict)          # scope closes as "withheld"
    d.evidence("bureau_pull", uri="cibil://req/88213", type="tool_call", content=bureau_json)
    d.model_call("anthropic", "claude-sonnet-5", tokens_in=6120, tokens_out=410, amount=2.11)
    d.act("approve", summary="Approve personal loan LN-20431", cost_centre="retail-lending")

# later, from the loan management system
w.outcome(subject="LN-20431", label="performing", observed_at="2026-12-15T00:00:00Z")
```

What happens: `decide()` opens a record and closes it when the block exits, whether by `act()`, by falling through (status `withheld`) or by an exception (status `failed`, exception re-raised). Evidence is hashed in-process and stored by reference only; excerpts are opt-in and pass through redaction. Recording never blocks the agent: records are queued and written by a background thread to a local SQLite store (default `.warrant/records.db`, or `WARRANT_STORE`), and spilled to disk if the store is unavailable. The store assigns each record a sequence and a hash chained to the previous record in the stream, and refuses updates and deletes.

## Mandate checks

Policies are CEL expressions in YAML or JSON files, one policy per file, mapped to decision classes. Install the extra and point the client at the directory:

```
pip install "warrantai[policy]"
```

```yaml
# policies/CR-07.yaml
policy_id: CR-07
version: "2026.3"
classes: [credit.approve]        # exact classes, or prefixes such as credit.*
fail_mode: closed                # closed | open | escalate, applied when a clause cannot evaluate
default: escalate                # result when no clause matches
clauses:
  - id: "4.1"
    title: Decline below bureau floor
    when: bureau_score < 650
    result: deny
  - id: "4.2"
    title: Auto-approve up to 5,00,000 when bureau score >= 720 and FOIR <= 45%
    when: amount <= 500000 && bureau_score >= 720 && foir <= 0.45
    result: allow
tests:
  - name: within limit auto-approves
    inputs: {amount: 450000, bureau_score: 748, foir: 0.38}
    expect: allow
    clause: "4.2"
```

```python
w = Warrant(stream="lending", policy_bundle="./policies", ...)
```

`check(**inputs)` evaluates clauses in order and the first match decides. The verdict's policy id, version, clause and reason land in the record's `mandate`. If a clause cannot evaluate, for example because an input is missing, the policy's fail mode decides and the record is flagged for review. A class no policy governs returns `unchecked`. Only `allow` is `allowed`; `unchecked` is not.

```
warrant policy test ./policies                                   # runs the tests embedded in each file
warrant policy check ./policies credit.approve amount=450000 bureau_score=748 foir=0.38
```

Without the extra, `Warrant(policy=...)` accepts any object with `evaluate(decision_class, inputs) -> Verdict`.

```
warrant export --store .warrant/records.db -o export.jsonl
warrant verify export.jsonl          # lending: 1,204 record(s), chain OK
warrant validate examples/loan-approval.json
warrant schema
```

## What arrives next

- budget envelopes: cost ceilings per decision, workflow and day, enforced through `check()`
- signed policy bundles published by a policy service and cached by the SDK
- automatic evidence capture from OpenTelemetry generative-AI spans inside a decision scope (`warrant.current_decision()` is the hook)
- `warrant test`: replay a set of real decisions against a changed target and fail the build on flipped decisions
- `warrant import`: reconstruct decision records from existing trace exports and run policy over them retrospectively
- collector and self-hosted PostgreSQL store

Roadmap and schema specification: https://warrantai.dev

## Note on the import name

The package installs as `warrantai` and imports as `warrant`. An unrelated, unmaintained package named `warrant` (an AWS Cognito helper, last released in 2017) also uses the `warrant` module name. Installing both in one environment will shadow one of them. Uninstall that package if `from warrant import validate` fails.

## Licence

Apache 2.0.
