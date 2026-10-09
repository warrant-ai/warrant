# warrantai

Warrant is the decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

This is the 0.10.0 line: the decision record schema, the `decide()` SDK, policy bundles, a collector and a PostgreSQL store for shared deployments and adapters for the Claude Agent SDK, LangGraph and Temporal. New in 0.10.0: a document that is on file but was not relied on can be recorded as an input only, so it is never matched to an obligation or counted as evidence. The Python and JavaScript SDKs are now tested against each other on random records in CI. See [CHANGELOG.md](https://github.com/warrant-ai/warrant/blob/main/CHANGELOG.md) for the complete release history.

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

## Outcomes: what actually happened

A decision record without an outcome is a log. The realised result — an alert reopened, a loan defaulted, a promise to pay kept — arrives days or months later and lands in someone else's system, so attaching it has to be something an operations team can do from a file rather than something an engineer does in code.

```
warrant outcomes ingest reopened-q3.csv --store .warrant/records.db --stream aml --source case-system
```

```
read 300 row(s) from 1 file(s)
wrote 300 outcome record(s) to stream 'aml'
  stayed_closed: 189
  reopened: 111
  outcome-attached share in stream 'aml': 300/400 (75.0%)
```

The file needs a `label` column and either `subject` or `decision_record_id`; `observed_at`, `score` and `source` are optional. Each row becomes a linked `outcome` record — nothing is mutated, because the ledger is append-only. Outcome ids are derived from the decision, the label and the observation time, so re-sending last week's file writes nothing new, while a *corrected* label lands as a later record and both survive. Rows that match no decision are reported rather than raised: an operations export routinely reaches outside the window, and that is a finding, not a failure. `--dry-run` reports without writing.

Nothing here cares whether the decision was recorded live or reconstructed by `warrant import`, so history whose outcomes are already known can be joined in one pass — which is the difference between a reliability curve in week one and one in month six.

`warrant outcomes status` prints the outcome-attached share on its own, overall and per decision class. It is the depth metric behind every calibration claim and belongs next to any of them.

## Calibrate: did the stated confidence hold?

A decider that says 0.90 should be right about nine times in ten. Warrant holds both halves of that sentence — the confidence recorded at decision time in `decision.answers[]`, and the outcome recorded months later — so the claim can be measured on your own book instead of taken from a vendor's benchmark.

```
warrant calibrate --store .warrant/records.db --stream aml \
  --correct-when "outcome.label == 'stayed_closed'" --by question_set
```

```
calibration for stream 'aml'
  correct when: outcome.label == 'stayed_closed'
  400 decision(s), 400 with confidence, 300 with an outcome (75.0% attached), 300 usable
  stated 0.741 vs observed 0.630   ECE 0.1133   MCE 0.2022   Brier 0.2279
  band          n   stated  observed   gap
  0.50-0.60     69   0.550     0.348  -0.202
  0.60-0.70     64   0.650     0.656  +0.006
  0.70-0.80     52   0.750     0.731  -0.019
  0.80-0.90     56   0.850     0.714  -0.136
  0.90-1.00     59   0.950     0.763  -0.187
  by question_set:
    aml.alert@3.1.0: n=300  stated 0.741  observed 0.630  ECE 0.1133
```

A negative gap means the decider overstated itself in that band, which is the shape that matters: it says the high-confidence band you were about to automate is not as good as it claims.

What counts as *right* is never inferred. You state it as a CEL expression over the joined record, because guessing which outcomes vindicate a decision is the kind of quiet assumption that makes an evidence product worthless. `--by` breaks the curve down by `class`, `question_set`, `route` or `inputs.<field>` — per-segment calibration is usually where a single safe-looking number falls apart. Decisions with no outcome yet are excluded from the curve and counted in the header, so the coverage the number rests on is always visible beside it.

`--max-ece` and `--max-mce` turn it into a CI gate, and `--report FILE` writes JSON. Predicates need the `policy` extra (`pip install "warrantai[policy]"`).

## Effective-dated policies, and breakers

**Policies are never edited in place.** A new version takes effect from a date, and a decision is
judged by the version that was in force **when it was made** — not by the one in force when someone
reads the record:

```yaml
policy_id: AML-01
version: "2026.2"
classes: [aml.alert.disposition]
effective_from: 2026-10-01      # the previous version carries effective_to: 2026-10-01
```

Several dated versions of the same policy now live in one bundle. Two versions whose windows
overlap is a load error, because then neither answer is right.

This is what makes replaying history honest. A decision made in June, replayed in March after the
auto-close threshold was raised, is still evaluated against June's policy:

```
record made 2026-06-15, replayed today
  judged by: AML-01@2026.1
  mandate allow -> allow, flipped=False
```

Without dates, that replay reports a flip and the diff says the decider changed when only the
calendar did. `warrant import` does the same for reconstructed decisions: they are historical by
definition, so they are judged by the rules that applied when they happened.

### Breakers

"No more than seventy per cent of alerts may be auto-closed" is not a statement about one decision,
so it is not a policy clause — the CEL engine has no history handle, by design. It lives beside the
policy and is evaluated after the clause has spoken:

```yaml
breakers:
  - class: aml.alert.disposition
    metric: auto_share          # or escalation_share
    window: 24h
    ceiling: 0.70               # or floor, for a queue that has gone quiet
    min_decisions: 200          # a breaker that fires on the third decision is noise
```

```python
from warrant.breaker import Breaker

adapter = DecisionAdapter(w, model, breaker=Breaker.load("breakers.yaml", store, stream="aml"))
```

A trip forces the decision to a person, sets `mandate.flagged` and writes the reason onto the
record, so it lands in the console's escalation queue and explains itself:

```
breaker: auto_share 0.900 is above 0.7 over 24h (200 decisions)
```

**A breaker may take a decision away from the machine and may never hand one to it.** `action:
allow` is not configurable and never will be, so a breaker that is itself broken fails towards a
human. `warrant breaker check` evaluates the rules against a store on demand, for a cron entry or
an alerting hook.

## Golden sets: catching a model that changed underneath you

A hosted model is updated without your consent. If the update moves confidences without changing
any decision, **nothing flips** — no policy result changes, no alert fires — and every calibration
threshold you tuned against the old numbers is quietly wrong. The reliability curve you showed a
validation committee last quarter describes a model that no longer exists.

A golden set is a fixed set of real decisions replayed against the version you pinned, every day:

```
warrant test golden --against jev-pinned \
  --fail-on answer-drift,confidence-drift,model-drift --junit golden.xml
```

```
# 20 decisions replayed (frozen) against jev-pinned
# 0 flipped
# 17 served by a different model (17 typesafe/jev-1.13.0 -> typesafe/jev-1.13.1)
# 17 confidence(s) moved, worst disposition: confidence 0.9 -> 0.935 (+0.035)
# FAILED: 17 decision(s) whose confidence moved; 17 decision(s) served by a different model
```

Zero flipped, and it still fails. That is the whole point.

Three signals, each its own gate:

| Gate | What it catches |
|---|---|
| `model-drift` | The model identifier on the record changed **without the target asking for it**. Replaying against a target that names a different model is ordinary replay, not drift — you asked for that. A pinned version serving something else is the finding |
| `answer-drift` | A typed answer's value changed, or a question stopped being answered |
| `confidence-drift` | A confidence moved past `--confidence-tolerance` (default 0.01) while the value stayed the same. The quiet one |

This is not a subsystem — it is `warrant test` with three more gates, so a golden set is just a
decision set, a pinned target and a cron entry, and it emits the same JUnit any CI runner reads.

A vendor that promises immutable pinned versions is making a claim you can check. This is how.

## Question sets: what was asked, versioned

A decision model answers preset typed questions. Silently editing one of them invalidates every
historical comparison that depended on it — the reliability curve you showed a validation committee
last quarter was measured against wording that no longer exists, and nothing in the record would say
so. So question sets are versioned files in your repository, and the version is stamped into every
record.

```yaml
# question-sets/aml.alert@3.1.0.yaml
id: aml.alert
version: "3.1.0"
owner: fiu-ops@bank.example
questions:
  disposition:
    primitive: choice
    instructions: Should this alert be closed as unremarkable, or escalated for investigation?
    criteria: [close, escalate]
  structuring_pattern:
    primitive: noul
    instructions: Is there a pattern of transactions structured below the reporting threshold?
```

Several versions live side by side, which is what makes a change reviewable:

```
warrant questions lint ./question-sets
warrant questions diff ./question-sets aml.alert@3.0.0 3.1.0
warrant questions show ./question-sets aml.alert
```

`lint` walks every consecutive pair of versions, classifies what moved, and **fails when the bump
was too small for the change it carries**. That is the CI gate:

```
$ warrant questions lint ./question-sets
1 question set(s), 2 version(s)
  aml.alert: 3.1.0, 3.1.1
  aml.alert@3.1.0 -> aml.alert@3.1.1
    [breaking] structuring_pattern: question removed
    breaking change; needs a major bump, got patch (INSUFFICIENT)
  1 problem(s):
    ... is a breaking change carried by a patch bump; needs major
```

A question removed, a primitive changed, or permitted answers narrowed is **breaking** — records
written under the old version can no longer be interpreted. Editing the instructions is
**semantic**: nothing breaks structurally, but the model is being asked a different thing, so the
answers are no longer comparable with the ones before. That is the change most likely to be made
carelessly, and the reason this exists.

Give the registry to the adapter and the pin becomes enforceable — an unregistered set cannot run,
and an answer outside the permitted values is caught where it happened rather than as a distortion
in a curve months later:

```python
from warrant.questions import Registry

adapter = DecisionAdapter(w, model, registry=Registry.load("./question-sets"))
```

`warrant pack --questions ./question-sets` then carries the sets the records actually cite into the
evidence pack, so a reader who sees `aml.alert@3.1.0` on a record can read the questions behind it
rather than an opaque token. A cited version the registry no longer holds stops the pack: a record
whose questions cannot be produced is a record nobody can interpret.

## Pack: the artefact a committee reads

Everything above produces evidence. This assembles it into something you can hand to internal audit,
a model validation committee or a supervisor — and that a customer can extract without us, which the
RBI's outsourcing directions require of anything a bank depends on.

```
warrant pack --store .warrant/records.db --stream aml -o ./q2-pack \
  --policy ./policies \
  --correct-when "outcome.label == 'stayed_closed'" --where "decision.route == 'auto'" \
  --answer disposition --by inputs.segment
```

The pack holds `records.jsonl` (every sealed record in the period), `manifest.json` (chain head,
counts, the policy and question-set versions in force), `coverage.json`, `calibration.json`, the
policy text that applied, and a `README.md` front page written for a reader who will never open a
terminal.

The chain is verified before anything reaches disk: a pack that fails its own verification is never
written, and a non-empty output directory is refused rather than overwritten.

The front page states what the chain proves and what it does not. A hash chain shows these records
have not been altered since they were sealed relative to one another; it does not show that whoever
operates the store could not have re-sealed the whole chain. Per-writer signing and external
anchoring are on the roadmap and are not implemented, and the pack says so rather than implying
otherwise — an evidence store whose operator could rewrite it undetected is not independent
evidence, and claiming otherwise is the fastest way to lose a validation committee.

Verify one with nothing but the CLI:

```
cd q2-pack && warrant verify records.jsonl
```

A worked example with a deliberate calibration failure is in `examples/gallery/aml`.

## Agent Decision Record: signed, warranted, attested

The Agent Decision Record (ADR) is the open format these records follow: a record of one consequential decision and the evidence that authorised it, which a third party can verify offline without trusting the system that wrote it. The specification is `spec/adr-0.2.md` (CC BY 4.0). Everything below is optional; a record that uses none of it is an ordinary Warrant record.

```
pip install "warrantai[sign,policy]"
warrant keys generate --issuer demo-bank --out keys/     # keys/demo-bank.key stays put; publish keys/demo-bank.keys.json
```

**Signing (level 1).** Give the store that seals records a key and every record carries the issuer's Ed25519 signature over its sealed hash: `Warrant(..., signing_key="keys/demo-bank.key")`, `warrant collector --signing-key`, or `$WARRANT_SIGNING_KEY`. A client that sends records to a collector does not sign; the collector does.

**Obligations and the warrant (level 2).** The relying organisation's own policy says what has to be true, from which providers, and how fresh:

```yaml
# policies/CR-09.yaml
enforce: true                        # acting without a warrant raises NotWarranted
retention: {class: rbi-credit-8y, period: 2922d}
obligations:
  - {id: OB-1, requires: tool_call, name: bureau_pull, providers: [cibil, experian], max_age: 30d}
  - {id: OB-2, requires: record, providers: [partner-data], max_age: 7d}
  - {id: OB-3, requires: human_review, when: amount > 2500000}
```

```python
with w.decide("credit.msme.approve", subject="LN-7731") as d:
    d.check(amount=2_000_000, bureau_score=742)
    d.evidence("bureau_pull", uri="cibil://req/55120", type="tool_call", provider="cibil",
               content=bureau, retrieved_at=pulled_at)
    d.cite(partner_record, keyring=Keyring.load("partner-data.keys.json"))   # the partner's signed record
    d.commit("approve")          # warranted -> committed; otherwise NotWarranted, and the record says why
```

Seven rules decide whether an item counts: no self-attestation, qualified providers, freshness, a digest, cited parents that were warranted, the right type, and a named human linked by digest to exactly what they were shown. Each rejection is written onto the record with its reason, and the verdict (`proposed`, `pending_evidence`, `escalated`, `warranted`, `refused`, `committed`) is recomputed by any verifier. A later change of state is a new `transition` record: `w.transition(record_id, "warranted", decided_by="human:officer-7", reviewer="officer-7", shown=[digest])`. A transition can supply a person, never missing evidence.

**Sensitive evidence.** `d.evidence(..., sensitive=True)` records a salted digest; the salt goes to the store's sidecar, never onto the record. `warrant erase --store ... --hash <digest>` deletes the sidecar entry: the record still verifies and the digest can no longer be linked to any value. `warrant evidence check --hash <digest> --file artefact.json --json --salt <hex>` shows an artefact produced a recorded digest.

**Checkpoints and witnesses (level 3).** A checkpoint is the issuer's signed RFC 9162 Merkle root over a whole chain. A witness holds no records; it co-signs only a checkpoint provably consistent with the last one it signed, so an issuer that rewrote its own history cannot get a second signature:

```
warrant checkpoint create --store bank.db --stream lending --key keys/demo-bank.key --previous last.json -o cp.json
warrant checkpoint cosign cp.json --key witness.key --keys demo-bank.keys.json --state witness-state/
warrant checkpoint anchor cp.json          # optional: OpenTimestamps, pending until Bitcoin confirms
warrant verify bank.jsonl --keys demo-bank.keys.json --keys partner-data.keys.json --keys witness.keys.json \
    --parents partner.jsonl --checkpoint cp.json --require-level L3
warrant trace 01M3GTSDHV4DC0D5AEMMVXR09W --export bank.jsonl --export partner.jsonl --keys ...
```

`warrant verify` reports the level each chain reaches and why it stops there. `warrant trace` walks a decision's parent links across organisations' exports and names the first step whose records show a problem. `warrant pack --keys ... --checkpoint ... --parents ...` ships the keys and checkpoints in the evidence pack and states its limits for the level reached.

What this does not prove: a signature does not stop an issuer rewriting its own history before a witness saw it; locating a faulty step is not assigning liability; and whether a record is admissible in court needs a legal opinion. `examples/adr/run_adr.py` runs the whole flow with two organisations and a witness.

The JavaScript SDK writes the same record fields and verifies signatures and Merkle proofs; obligations, the warrant and transitions are Python-only in this release.

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

## Adapters: Claude Agent SDK, LangGraph and Temporal

If your agent is built on a framework, you do not have to wrap each decision by hand. Most tool calls are not decisions, so you name the tools that are, and how to read a decision out of each call:

```python
from warrant.adapters import ToolDecision

decisions = {"approve_loan": ToolDecision("credit.approve", subject="loan_id", action="approve",
                                          inputs=["amount", "bureau_score", "foir"])}
```

Before a mapped tool runs, its arguments are checked against the policy. `deny` stops the call and tells the model which policy and clause said no. `escalate` stops it too, or hands it to a human, as below. `allow` changes nothing: an adapter only ever restricts, it never approves something the framework would have asked about. After the tool runs the decision is recorded, and the other tool results seen in the same session since the last decision are attached as evidence, by hash. A tool that fails is recorded as a failed decision, without its error text. A call the adapter cannot read (no subject, say) is blocked and is not a decision.

**Claude Agent SDK** (`pip install "warrantai[claude-agent,policy]"`):

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from warrant.adapters.claude_agent import WarrantHooks

guard = WarrantHooks(w, {"mcp__bank__approve_loan": decisions["approve_loan"]}, pricer=my_price_table)
options = ClaudeAgentOptions(hooks=guard.hooks(), ...)      # guard.hooks(existing) keeps hooks you already have
async for message in query(prompt=..., options=options):
    guard.observe(message)                                  # optional: puts model usage and cost on the next decision
```

`on_escalate="deny"` (the default, for unattended agents) blocks an escalation; `on_escalate="ask"` hands it to the host's own permission prompt, and the record says whether a person approved it. By default only `mcp__*` tools count as evidence, so file reads and shell commands do not flood a record.

**LangGraph** (`pip install "warrantai[langgraph,policy]"`):

```python
from langgraph.prebuilt import ToolNode
from warrant.adapters.langgraph import WarrantToolGuard

guard = WarrantToolGuard(w, decisions, on_escalate="interrupt")
tools = ToolNode([approve_loan, bureau_pull], wrap_tool_call=guard.wrap, awrap_tool_call=guard.awrap)
```

With `on_escalate="interrupt"` an escalation pauses the graph with LangGraph's `interrupt()`. Nothing runs and nothing is recorded while it waits. Resume with `Command(resume={"approve": True, "reviewer": "asha@bank.example"})`: the tool runs, and the reviewer's verdict is appended as its own sealed record. The default, `"block"`, returns an error message to the model and records the decision as withheld. Evidence is collected per `thread_id`, and only when there is one, so one customer's lookups can never land on another's decision.

**Temporal** (`pip install "warrantai[temporal,policy]"`):

```python
from temporalio.worker import Worker
from warrant.adapters.temporal import WarrantInterceptor, WarrantPlugin

guard = WarrantInterceptor(w, {"disburse": ToolDecision("credit.disburse", subject="loan_id",
                                                        inputs=["amount", "bureau_score", "foir"])})
worker = Worker(client, task_queue="lending-agents", workflows=[LoanApproval],
                activities=[underwrite, disburse], plugins=[WarrantPlugin(guard)])
```

The plugin installs the interceptor, registers the `warrant.record` local activity for the workflow-side helpers below, and lets workflow code import `warrant` without `workflow.unsafe.imports_passed_through()`; give it to `Replayer(plugins=[...])` too. Without the plugin, pass `interceptors=[guard]`, add `guard.record_activity` to `activities`, and import the helpers under `imports_passed_through()`. The full guide, with the test plan, is `integrations/temporal.md`.

Workflow code does not change: the decisions are the activities you name, keyed by activity type, and their arguments are read by parameter name (a single dataclass argument, field by field). A denied or escalated activity is recorded as withheld and fails with a non-retryable `ApplicationError` of type `WarrantDenied` or `WarrantEscalated`, so Temporal's retry policy does not re-run it and the workflow can catch it and hand the case to a person; the error's details carry the record id. Every record names the Temporal execution (namespace, workflow, run, activity, attempt) as evidence, so an auditor can open the run. One record per attempt: a failed attempt is a failed decision, a retry is a new record, and record ids are derived from the attempt's identity, so a batch delivered twice is written once. The run's other activities are evidence for its next decision, by hash, on the worker that ran them. `model_usage(provider, model, tokens_in=..., tokens_out=...)` called inside an activity puts the model call's cost on that activity's record. The policy check is in-process and recording is asynchronous, so Warrant being unreachable never touches an activity.

Workflow code can make decisions of its own and hand approvals back to escalated activities:

```python
from warrant.adapters import temporal_workflow as warrant

@workflow.defn
class LoanApproval:
    @workflow.run
    async def run(self, loan):
        if workflow.patched("warrant-underwrite-decision"):
            verdict = await warrant.decide("credit.approve", subject=loan.loan_id, inputs={...}, action="approve", summary=reason)
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

`decide()` records the workflow's own decision through a local activity, so Temporal keeps its result in history and replay never writes it twice; it returns the verdict to branch on. Its ids come from `workflow.uuid4()`, so a call added to a running workflow shifts what follows: put new call sites behind `workflow.patched`. `escalation(exc)` reads the escalated decision out of the activity error (a denial is not an escalation; nobody can approve it). `approved()` runs the activity again with the reviewer's name attached: the verdict is appended as its own sealed record, linked to the escalated decision, before the activity runs, and the activity's record names the reviewer. Who the reviewer is comes from your own Update or Signal payload; Warrant records it and does not authenticate it. `rejected()` records a rejection, or a timed-out wait.

## Laya: a decision model that runs inside your perimeter

Laya is an open-weight decision model from Convai Innovations (Apache 2.0). It answers the same noul, choice and score questions as TypeSafe's Jev, but runs in your own process from weights on local disk, so the state it reads never leaves the host. Use it where customer data may not be sent to a model hosted elsewhere.

```
pip install "warrantai[laya]"
```

```python
from warrant.adapters.laya import LayaAdapter, LayaModel

model = LayaModel(revision="55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851", subfolder="multilingual")
adapter = LayaAdapter(w, model, residency={"aml.alert.disposition": "IN"})
result = adapter.decide(decision_class="aml.alert.disposition", subject="alert:8812",
                        state=alert, questions=registry.get("aml.alert", "3.1.0").questions)
```

- **Pin a commit.** `revision` must be a 40-character Hugging Face commit sha. The adapter downloads that exact commit and loads it from disk, because `laya.load` takes no revision and the branch moves when Convai publishes. Without a pin it refuses unless you pass `allow_unpinned=True`.
- **The record names what answered.** Laya reports `laya-rl-agent` for every checkpoint and version. The record carries `laya@<sha[:12]>/<checkpoint>` instead, for example `laya@55cf4c4ebb4e/multilingual`.
- **Confidence is the probability of the answer.** Laya's own `confidence` field is normalised entropy, not a probability. A choice records `probabilities[chosen]`, a noul records the winning side's probability, and a score records no confidence, as with Jev.
- **Residency.** The endpoint is recorded as `local://laya?region=in-process`. In-process inference satisfies any `residency=` requirement, since nothing is sent.
- **Train it first.** Out of the box Laya is close to random on unfamiliar questions; Convai's own guidance is to fine-tune and refit its temperature on your data. Measure it with `warrant calibrate` on your own outcomes before any clause routes on its confidence.

Questions can be `warrant.questions.Question` objects, their dict form, or Laya's own `{type, instructions, criteria}` dicts. A malformed question is refused before a decision opens, and a model error records only its class, never its message.

## MCP: for agents you do not write the code for

Agents built in a host that speaks the Model Context Protocol can use Warrant without the SDK. The server runs over stdio, so the host launches it:

```
pip install "warrantai[mcp,policy]"
```

```json
{"mcpServers": {"warrant": {"command": "warrant", "args": ["mcp", "--stream", "lending", "--policy", "policies/",
  "--agent-name", "credit-underwriter", "--agent-version", "2.4.0", "--store", "https://collector.internal"],
  "env": {"WARRANT_TOKEN": "..."}}}}
```

| Tool | When | What it does |
|---|---|---|
| `describe_mandate` | any time | The written policy for a decision class: clauses in order, the default, the fail mode |
| `check_mandate` | before acting | Evaluates the policy on the inputs. Records nothing. `allowed` is true for `allow` only |
| `record_decision` | after acting, or after holding back | Records the decision with evidence, model calls and cost. Returns the `record_id` |
| `record_outcome` | when the result is known | Links an outcome to the decision by `record_id` or subject |

What the agent cannot do matters as much. Its identity comes from the server's flags, never from a tool argument. The mandate on a record is always evaluated by the server from the inputs and cannot be supplied, so an agent that reports acting where the policy said no is recorded exactly that way, with `outside_mandate: true`, and queues for human review. There is no tool for a human verdict. A malformed call is refused with a reason and records nothing. Evidence content is hashed in the server and never stored.

## What arrives next

- budget envelopes: cost ceilings per decision, workflow and day, enforced through `check()`
- signed policy bundles published by a policy service and cached by the SDK

Roadmap and schema specification: https://warrantai.dev

## Note on the import name

The package installs as `warrantai` and imports as `warrant`. An unrelated, unmaintained package named `warrant` (an AWS Cognito helper, last released in 2017) also uses the `warrant` module name. Installing both in one environment will shadow one of them. Uninstall that package if `from warrant import validate` fails.

## Licence

Apache 2.0.
