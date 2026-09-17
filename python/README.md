# warrantai

Warrant is the decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

This is the 0.2.0 line: the decision record schema, the `decide()` SDK with a local append-only store and the offline verifier, policy bundles, replay and import, and, new in 0.2.0, a collector and a PostgreSQL store for shared deployments.

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
    when: amount <= 500000 && bureau_score >= 720 && double(foir) <= 0.45
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

Two patterns are not portable between CEL engines, and the loader warns about both. Compare an input with a decimal through `double()`: write `double(foir) <= 0.45`, because a whole-number input such as `0` or `1` is an int and cel-python cannot compare an int with a double, so the fail mode would apply. Divide through `double()` too: whole numbers divide as integers, so `30000 / 50000` is `0`. The JavaScript SDK evaluates the same bundles, and both run the shared cases in `conformance/policy-cases.json`.

```
warrant policy test ./policies                                   # runs the tests embedded in each file
warrant policy check ./policies credit.approve amount=450000 bureau_score=748 foir=0.38
```

Without the extra, `Warrant(policy=...)` accepts any object with `evaluate(decision_class, inputs) -> Verdict`.

## Automatic evidence from OpenTelemetry

If your model and tool calls are already instrumented with the OpenTelemetry generative-AI conventions, register the span processor once and every such span that starts inside a `decide()` scope is attached to that decision as evidence, with token usage, when it ends:

```
pip install "warrantai[otel]"
```

```python
from opentelemetry.sdk.trace import TracerProvider
from warrant.otel import WarrantSpanProcessor

def price(provider, model, tokens_in, tokens_out):
    return tokens_in * 0.0003 + tokens_out * 0.0015     # your price table, in the client's currency

provider = TracerProvider()
provider.add_span_processor(WarrantSpanProcessor(pricer=price))
```

Model spans become `model_call` evidence and tool spans become `tool_call` evidence, each pointing at the span by trace and span id. The conventions carry tokens but not money, so cost is whatever `pricer` returns, or zero. Spans that start outside a decision, or end after it closed, are ignored. For code without OpenTelemetry, `d.model_call(...)` and `d.tool_call(...)` record the same thing by hand.

```
warrant export --store .warrant/records.db -o export.jsonl
warrant verify export.jsonl          # lending: 1,204 record(s), chain OK
warrant validate examples/loan-approval.json
warrant schema
```

## Replay: test a change against real decisions

Turn on capture where it is safe to do so (development and staging), and route tool calls through `d.tool()` so their results can be served back during replay:

```python
w = Warrant(stream="lending", policy_bundle="./policies", capture_inputs=True, capture_evidence=True, ...)

def decide(d, inputs, target=None):
    verdict = d.check(**inputs)
    bureau = d.tool("bureau_pull", lambda: cibil.pull(inputs["subject"]), uri=f"cibil://req/{inputs['subject']}")
    d.model_call("anthropic", target.model if target else "claude-sonnet-5", tokens_in=..., tokens_out=..., amount=...)
    d.act("approve" if verdict.allowed and bureau["score"] >= 720 else "refer")
```

`capture_inputs` stores the `check()` inputs on the record; `capture_evidence` keeps tool results in the local store's blob table, keyed by the same content hash the record carries. The sealed record itself still holds evidence by hash and reference only.

Then, before shipping a change:

```
warrant set create lending-edge --store .warrant/records.db --from-stream lending --where "outcome.label == 'default'" --limit 200
warrant target add underwriter-v2.4 --agent credit-underwriter@2.4.0 --model anthropic/claude-haiku-4-5-20251001 --policy ./policies --decider underwriter:decide
warrant test lending-edge --against underwriter-v2.4 --mode frozen --fail-on flipped,new-deny --max-cost-increase 10% --junit report.xml
```

```
# 200 decisions replayed (frozen) against underwriter-v2.4
# 7 flipped (5 approve -> refer, 2 refer -> approve)
#   by recorded outcome: 5 default, 2 performing
# 1 new deny (CR-07 clause 4.1)
# cost 3.84 -> 0.90 per decision (-77%)
# FAILED: 7 flipped decision(s) exceed threshold 0
```

The decider receives the recorded `check()` inputs, or the full payload if live code called `d.set_inputs(payload)`; the subject is `d.subject`. A set is a portable JSONL file of real decisions with their outcomes joined. A target names what changes: agent version, model, policy bundle, and the decider, a `module:function` that makes one decision with the same `d` API as live code. In `frozen` mode `d.tool()` returns the recorded result and only model calls run; in `live` mode tools run again. A decision whose new code calls a tool the recording never saw is reported as unreplayable rather than silently run live. Replayed records never enter the ledger. Gates: `--fail-on flipped,new-deny,new-escalate,unreplayable` and `--max-cost-increase PCT`; a decider that raises always fails the run. Reports as JSON and JUnit for CI.

## Import: start from the traces you already have

If your agents already emit OpenTelemetry generative-AI spans, you do not have to instrument anything to get a first finding. A taxonomy names which side-effecting tool spans are decisions and how to read the subject, action and inputs from their attributes:

```yaml
# taxonomy.yaml
stream: lending-import
agent: {name_attr: service.name, version_attr: service.version}
pricing: {anthropic/claude-sonnet-5: {input_per_1k: 0.25, output_per_1k: 1.25}}
decisions:
  - class: credit.approve
    match: {tool: approve_loan}
    subject: gen_ai.tool.call.arguments.loan_id
    action: approve
    inputs: gen_ai.tool.call.arguments
```

```
warrant import traces.jsonl --taxonomy taxonomy.yaml --store .warrant/records.db --policy ./policies
```

```
read 24 span(s) in 6 trace(s) from 1 file(s)
wrote 6 record(s) to stream 'lending-import' with origin: imported
  credit.approve: 6 decision(s), 6 with inputs, 6 with model calls checked against COL-02@2026.1, CR-07@2026.3
    allow 3   deny 1   escalate 2   unchecked 0
  3 decision(s) outside mandate:
    LN-30002  escalate  CR-07 clause 4.3  Refer tickets above 5,00,000 to a credit officer
    LN-30003  deny  CR-07 clause 4.1  Decline below bureau floor
    LN-30004  escalate  CR-07 default  no clause matched
```

Accepted inputs are OTLP JSON or JSONL as written by the collector's file exporter, and the Python SDK's console-exporter JSON. Every matched span becomes one record with `origin: imported`; the other generative-AI spans in its trace become evidence by reference, with token usage and, if the taxonomy has a price table, cost. Record ids are derived from the trace and span ids, so importing the same export twice writes nothing new. Imported records are chained like any other, and `origin` keeps them distinguishable from records sealed at decision time. `--dry-run` reports without writing; `--report FILE` writes JSON.

## Production: the collector and PostgreSQL

For a shared, self-hosted store, run the collector in front of PostgreSQL and point agents at it. The collector is stateless; run as many as you like behind a load balancer. Chaining is serialised per tenant and stream inside PostgreSQL.

```
pip install "warrantai[collector]"
warrant collector --store postgresql://warrant:...@db:5432/warrant --listen 0.0.0.0:8787 --token demo-bank:<at least 16 chars>
```

```python
w = Warrant(stream="lending", tenant="demo-bank", store="https://collector.example.internal", token=os.environ["WARRANT_TOKEN"], ...)
```

Records are batched, gzip-compressed and sent asynchronously; if the collector is down they spill to disk and are retried. A bearer token belongs to one tenant, and a batch may only carry that tenant's records. `warrant export`, `warrant set create` and `warrant import` accept a `postgresql://` URL wherever they accept a store path. `deploy/docker-compose.yml` starts PostgreSQL and one collector; `deploy/Dockerfile` builds the collector image.

Endpoints: `POST /v1/records`, `GET /healthz`, `GET /readyz` (checks the database), `GET /metrics` (Prometheus text). Limits: 1000 records or 8 MiB per batch.

Stores written by 0.1.0 chained per stream; 0.2.0 chains per tenant and stream and migrates a local store on first open.

## What arrives next

- budget envelopes: cost ceilings per decision, workflow and day, enforced through `check()`
- signed policy bundles published by a policy service and cached by the SDK

Roadmap and schema specification: https://warrantai.dev

## Note on the import name

The package installs as `warrantai` and imports as `warrant`. An unrelated, unmaintained package named `warrant` (an AWS Cognito helper, last released in 2017) also uses the `warrant` module name. Installing both in one environment will shadow one of them. Uninstall that package if `from warrant import validate` fails.

## Licence

Apache 2.0.
