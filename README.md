# Warrant

The decision ledger for AI agents. Every consequential action an agent takes is recorded with the mandate that allowed it, the evidence it used, what it cost, and how it turned out. Developers replay real recorded decisions against a changed prompt, model or policy before shipping. Risk, finance and audit teams get records they can sample and verify without trusting the vendor.

Status: pre-alpha. Release 0.0.1 publishes the decision record schema and a validator in Python and JavaScript so the format can be reviewed before the SDK lands.

| Package | Install | Import |
|---|---|---|
| Python | `pip install warrantai` | `from warrant import validate` |
| JavaScript | `npm install warrantai` | `import { validate } from "warrantai"` |

Both install a `warrant` command with `--version`, `schema` and `validate FILE...`.

## Layout

- `schema/` canonical JSON Schema for the decision record. Edit here, then run `scripts/sync-schema.sh` to copy it into both packages.
- `examples/` records that validate against the schema, used by both test suites.
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
