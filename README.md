# Warrant

The decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

Status: pre-alpha. 0.1.0 shipped the Python `decide()` SDK, CEL policy checks, the local append-only store and verifier, replay with `warrant test`, OpenTelemetry evidence capture, and `warrant import` for existing trace exports; see `python/README.md`. 0.2.0 adds the collector and a PostgreSQL store for shared deployments, the JavaScript and TypeScript SDK (`js/README.md`), and policy bundles that evaluate identically in both languages (`conformance/`).

| Package | Install | Import |
|---|---|---|
| Python | `pip install warrantai` | `from warrant import validate` |
| JavaScript | `npm install warrantai` | `import { validate } from "warrantai"` |

Both install a `warrant` command with `--version`, `schema` and `validate FILE...`.

## Ten minutes to a first verified record

```
pip install "warrantai[policy]"
```

```python
from warrant import Warrant, AgentInfo

w = Warrant(stream="lending", agent=AgentInfo("credit-underwriter", "2.3.1"), policy_bundle="examples/policies", currency="INR")
with w.decide("credit.approve", subject="LN-20431") as d:
    verdict = d.check(amount=450000, bureau_score=748, foir=0.38)     # allow, CR-07 clause 4.2
    d.evidence("bureau_pull", uri="cibil://req/88213", type="tool_call", content={"score": 748})
    d.act("approve" if verdict.allowed else "refer", cost_centre="retail-lending")
w.close()
```

```
warrant export --store .warrant/records.db -o export.jsonl && warrant verify export.jsonl
```

Then see what a change does to real decisions, using the gallery:

```
cd examples/gallery/lending
warrant test lending-edge --against underwriter-v2.4 --fail-on flipped
```

Or start from traces you already have:

```
warrant import examples/import/traces.jsonl --taxonomy examples/import/taxonomy.yaml --policy examples/policies --dry-run
```

The full SDK guide is in `python/README.md`.

## Layout

- `schema/` canonical JSON Schema for the decision record. Edit here, then run `scripts/sync-schema.sh` to copy it into both packages.
- `examples/` records that validate against the schema, a policy bundle, a trace export with its import taxonomy, and the lending gallery; used by the test suites and the quick start.
- `python/` the `warrantai` Python package (module name `warrant`).
- `js/` the `warrantai` npm package.
- `docs/` scope and go-to-market working documents.

## Developing

```
cd python && python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest
cd js && npm install && npm test
```

## Licence

Apache 2.0.
