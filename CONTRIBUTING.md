# Contributing to Warrant

Warrant is Apache 2.0. The Agent Decision Record specification in `spec/` is CC BY 4.0. Contributions from people and from coding agents are both welcome, and the same gates apply to both. If you are a coding agent, or you are driving one, read `AGENTS.md` as well.

The repository is built so that "correct" is defined by files, not by a maintainer's opinion: the specification, the conformance vectors, the shared policy cases and a differential test between the two SDKs. A contribution that passes those gates is a contribution we can merge without having to trust who wrote it. That is also the point of the product.

## What is worth doing

In rough order of value to the project:

1. **A verifier in a third language.** Go, Rust, Java, .NET, Swift. The record format, the seal, the signature message, the key ids and the Merkle proofs are all pinned in `conformance/adr-vectors.json`, so a port is right when it reproduces those bytes. See "Porting the verifier" below.
2. **Findings.** A record one SDK seals and the other rejects. A CEL expression the two policy engines evaluate differently. A sentence in `spec/adr-0.2.md` that the code does not match. File it as a bug with the record, expression or sentence attached. These are the most useful issues this repository can receive.
3. **Adapters** for agent frameworks and decision models, behind the existing boundaries (`python/src/warrant/adapters/`, `js/src/adapters/`).
4. Issues labelled `good first issue`, `port`, `adapter` or `spec`. Issues labelled `agent-friendly` are specified completely by vectors or tests and suit a coding agent.

## Setting up

Python, 3.10 or later:

```
cd python
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

PostgreSQL tests skip unless `WARRANT_TEST_PG` points at a database (CI uses `postgres:17-alpine` on port 55432). Temporal tests download Temporal's time-skipping test server on first run. Live decision-model tests are off unless their environment variables are set; never set them in a contribution.

JavaScript, Node 18 or later (the policy engine needs 20.19, the Temporal tests 20.3):

```
cd js
npm ci
npm test
npm run typecheck
```

The differential test, which seals random records in one SDK and verifies them in the other:

```
pip install -e "python[sign]"
python scripts/differential.py --seed 7 --count 500
```

## The gates every pull request passes

CI runs on every pull request:

| Job | What it proves |
|---|---|
| `schema-in-sync` | `schema/decision-record.v0.json` is byte-identical to the copies inside both packages |
| `python` on 3.10, 3.12, 3.13 | The Python suite passes against PostgreSQL and the package builds |
| `js` on Node 18, 20, 22 | The JS suite passes, the type declarations compile, the package packs |
| `differential` | 20,000 random records sealed in Python verify in JS and the reverse, on a fresh seed each run |
| `action` | The replay GitHub Action produces exactly the documented outcomes against the lending gallery |

Both suites run the shared cases in `conformance/`: `policy-cases.json` (CEL expressions both engines must evaluate identically), `admissibility-cases.json` (the seven admissibility rules and the lifecycle, reason codes and messages included) and `adr-vectors.json` (hashes, seals, signatures, key ids, Merkle proofs).

## Rules the gates enforce, and a reviewer will hold you to

1. **The two SDKs move together on the record path.** A change to hashing, canonical JSON, sealing, signing, Merkle proofs, admissibility or the lifecycle changes `python/` and `js/` in the same pull request, and the vectors prove they still agree.
2. **Vectors are generated, never edited by hand.** `scripts/gen_adr_vectors.py` and `scripts/gen_admissibility_cases.py` produce them from the Python implementation. A change to a vector is a change to the specification: the pull request updates `spec/adr-0.2.md` and `CHANGELOG.md` and says why.
3. **A new CEL feature needs a case in `conformance/policy-cases.json` before any code relies on it.** If the two engines disagree on an expression, it goes under `divergent` with each SDK's pinned behaviour, not under `cases`.
4. **The schema is edited in `schema/` and copied with `scripts/sync-schema.sh`.** Fields are added as optional. No record written under schema version 0 may become invalid.
5. **Nothing from a vendor reaches the record.** Decision-model adapters live under `adapters/` behind the `DecisionModel` interface. A fake adapter and a real one are interchangeable with no change outside `adapters/`, and a test asserts it.
6. **Error messages are never recorded, only the exception class.** A message can quote the state a model read, which can hold personal data. Tests assert that a PAN inside a vendor error reaches neither the raised exception nor the ledger.
7. **Records are append-only.** No change may update or delete a sealed record. Corrections, outcomes and verdicts are linked records.
8. **The changelog entry says what was wrong before,** not only what is new. Read the existing entries for the shape.

## Porting the verifier

A verifier reads an export (JSON Lines, one record per line) and reports whether the chain is intact and the signatures valid, with nothing but the issuer's public keys. Build it in this order, making each vector group in `conformance/adr-vectors.json` pass before the next:

1. `canonical`: RFC 8785 canonical JSON. Numbers in ECMAScript form, keys sorted by UTF-16 code unit.
2. `salted`: SHA-256 content hashes, with and without a salt.
3. `seal`, `seal_jcs`, `seal_legacy`, `seal_sealed_at`: the record hash over the body without `seal`, the prev-hash chain, and the Ed25519 message `"adr/0.2 seal\n" + hash` or `... + "\n" + sealed_at` (section 5.1 of the spec says which applies to which record).
4. `key`: the key id derived from a public key. Section 5.2 also defines the key-set format with `not_before` and `revoked_at`, which a verifier reads but the vectors do not pin.
5. `merkle`, `checkpoint_message`: RFC 9162 roots, inclusion and consistency proofs, and the checkpoint signing message (section 6).

Then `admissibility-cases.json` if the port is going to recompute verdicts (level L2), which it need not for a first release. The spec names the conformance levels in section 7. Open an issue with the `port` template when the first group passes; we will link it from the README once the seal group passes.

## Pull requests

- One change per pull request. Say what was wrong before, what you changed, and the commands you ran.
- Tests go in with the change. Cover the case that motivated it and at least one way it can fail.
- Match the surrounding style. No reformatting of lines you did not need to touch.
- Say whether a coding agent wrote the change and which model. It is not held against the change; it tells the reviewer what to read for.
- There is no contributor licence agreement. By submitting a change you agree it is licensed under Apache 2.0, or CC BY 4.0 for changes to `spec/`.

Questions go in GitHub Discussions. Security problems go through `SECURITY.md`, not the issue tracker.
