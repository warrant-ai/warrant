# Agent Decision Record (ADR) — specification 0.2

Status: draft · 27 September 2026 · licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
The reference implementation is `warrantai` (Apache 2.0), which implements ADR 0.2 from its
0.7.0 release.

An Agent Decision Record is a tamper-evident record of one consequential decision made by a software
agent, with the evidence that authorised it. A third party holding the record and the issuer's
public key can verify it offline, without trusting the system that wrote it.

The principle is **share proofs, not data**. Records carry digests of what an agent read, never the
content itself. Organisations keep their data; what crosses a boundary is a signed record and a
Merkle root.

ADR 0.2 is expressed as optional fields on the Warrant decision record, schema version `0`
(`schema/decision-record.v0.json`). Every record written before this specification remains valid.
A record that uses none of the fields below is an ordinary Warrant record.

Keywords MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. What a record contains

| Part | Field | What it records |
|---|---|---|
| Actor | `actor.name`, `actor.version`, `actor.model`, `actor.runtime`, `actor.on_behalf_of`, `actor.identity` | Which agent, which version, which model and runtime, the legal principal it acts for, and optionally its identity in an external registry. |
| Inputs | `evidence[]` items with no `obligation`, `decision.state_digest` | Digests of what the agent read. |
| Proposal | `decision.action`, `decision.summary`, `decision.claims[]` | What the agent intends and what it asserts. Never authoritative on its own. |
| Obligations | `obligations[]` | What had to be true, each traced to a policy clause and marked `verifiable` or `advisory`. |
| Evidence | `evidence[]` items with `provider`, `obligation`, `admission` | Items from named providers, each admitted or rejected with a reason code. The only thing that can authorise. |
| Verdict | `verdict.state`, `verdict.decided_by`, `verdict.met`, `verdict.unmet` | The lifecycle state reached and who decided it. Recomputable by any verifier. |
| Handoffs | `parents[]` | Upstream records this decision relies on, by id and sealed hash. |
| Integrity | `seal.prev_hash`, `seal.hash`, `seal.signature`, `seal.key_id` | Position in the issuer's hash chain and the issuer's Ed25519 signature. |
| Retention | `retention.class`, `retention.retain_until`, `retention.legal_hold` | Record-keeping duty and legal hold. |

### 1.1 Actor

`actor.identity` is `{registry, id}` and MAY carry `uri`. It names the agent in a registry the
relying party recognises, for example a payment-system agent registry. ADR does not run a registry.

### 1.2 Claims

`decision.claims[]` is a list of `{claim, value}` the agent asserts, such as
`{"claim": "gst_turnover_inr", "value": 42000000}`. A claim is never evidence, including when a
downstream agent reads it from an upstream record (rule 5).

### 1.3 Evidence items

Each item has `name`, `type`, `uri` and `content_hash`, and MAY have:

- `provider`: who produced the item. The literal `self`, or a provider equal to `actor.name`,
  marks it as supplied by the acting agent.
- `retrieved_at`: when the provider produced or the agent fetched it.
- `obligation`: the obligation id the item is offered against. An item without one is an input.
- `admission`: `{status: admitted|rejected, reason}`. Written by the issuer, recomputed by verifiers.
- `salted`: `true` when `content_hash` is a salted digest (section 4).
- `parent`: for `type: record`, the cited record `{record_id, hash, issuer}`.

Types: `model_call`, `tool_call`, `document`, `web`, `other`, and in 0.2 `record` (an upstream ADR),
`mandate` (a signed delegation such as an AP2 mandate or a UPI mandate), `attestation` (a provider's
signed statement) and `human_review`.

### 1.4 Obligations

```json
{"id": "OB-2", "clause": "4.2", "policy_id": "CR-07", "policy_version": "2026.4",
 "requires": "record", "providers": ["partner-gst-agent"], "max_age_seconds": 2592000,
 "kind": "verifiable", "met": true, "satisfied_by": ["partner-gst"]}
```

`requires` is an evidence type. `name`, when present, further restricts which evidence names count.
`providers` lists the providers the relying organisation accepts; an empty or absent list accepts
any provider that is not the actor. `max_age_seconds` bounds freshness. A `verifiable` obligation
must be met for the decision to be warranted. An `advisory` one is recorded and never blocks.

Obligations come from the relying organisation's own policy. There is no global list of qualified
providers, so there is no monopoly at the evidence layer.

## 2. Admissibility rules

An evidence item satisfies an obligation only if it is **admitted**. A verifier MUST apply these
seven rules in order and record the first that fails as the rejection reason.

| # | Rule | Reason code |
|---|---|---|
| 1 | **No self-attestation.** The acting agent never supplies evidence for its own obligation. An item with no `provider` is treated as the agent's own. | `self_attested` |
| 2 | **Qualified providers.** Only providers the obligation names are accepted. | `unqualified_provider` |
| 3 | **Freshness.** Evidence older than `max_age_seconds` at the verdict time is rejected. Evidence without `retrieved_at` is rejected when a bound applies. | `stale`, `no_timestamp` |
| 4 | **Digest-bound.** Every admitted item carries a `content_hash`. | `missing_digest` |
| 5 | **Linked handoffs.** A `record` item is admitted only if the cited parent appears in `parents[]` with the same hash and its cited state is `warranted` or `committed`. | `parent_not_cited`, `parent_not_warranted` |
| 6 | **Type match.** The item's type is the obligation's `requires`, and its name matches when the obligation names one. | `wrong_type` |
| 7 | **Accountable humans.** A `human_review` obligation is met only by a named reviewer linked to the exact material shown, by digest (`human.reviewer`, `human.shown[]`), where every shown digest appears on the record. | `unnamed_reviewer`, `material_not_linked` |

## 3. Lifecycle

States: `proposed`, `pending_evidence`, `escalated`, `warranted`, `refused`, `committed`.

```
proposed ─┬─> pending_evidence ─┬─> warranted ──> committed
          ├─> escalated ────────┤
          ├─> warranted         └─> refused
          └─> refused
```

- `committed` is reachable only from `warranted`.
- `refused` and `committed` are terminal.
- `warranted` requires every `verifiable` obligation met, or a named human decision from
  `escalated` satisfying rule 7.
- A transition out of `pending_evidence` into `warranted` may only supply a person: it is allowed
  when every unmet obligation is a `human_review` and the transition satisfies rule 7. Missing
  evidence of any other kind needs a new decision record that cites this one.
- A decision record carries the state reached when its scope closed, with the path in
  `verdict.history`. A later change of state is a new record of type `transition` that references
  the decision. History is never edited.
- A mandate result of `deny` maps to `refused`, `escalate` to `escalated`, and `allow` to
  `warranted` or `pending_evidence` according to the obligations.

## 4. Privacy by construction

A plain SHA-256 of a low-variety artefact, such as a sanctions result `{"hits":0}` or a turnover
band, can be reversed by trying every value. Evidence marked sensitive therefore MUST use a
**salted digest**:

```
content_hash = SHA-256( salt || content_bytes )    salt = 32 random bytes
```

`content_bytes` is the content as hashed unsalted: bytes as is, strings as UTF-8, anything else as
canonical JSON (section 5.1). The item carries `salted: true`. The
salt is kept in the issuer's **sidecar**, next to any captured content, and never on the record.

- Signatures and the hash chain never need the artefact, so offline verification is unaffected.
- Anyone checking a digest already holds the artefact and is given its salt.
- **Erasure** deletes the sidecar entry. The personal data and its salt are gone, the signed record
  still verifies, and the digest can no longer be linked to any value.
- A sensitive item MUST NOT carry an `excerpt`. Personal attributes MUST NOT appear as cleartext in
  `decision.claims`, `decision.summary` or `decision.inputs`; issuers SHOULD redact them.

## 5. Integrity

### 5.1 Seal and signature

The issuer's store assigns `sequence`, sets `seal.prev_hash` to the previous record's hash in the
same `(tenant, stream)` chain, and computes

```
seal.canon = "jcs"
seal.hash  = SHA-256( canonical_json(record without seal) || "\n" || prev_hash_or_empty )
```

`canonical_json` is RFC 8785, the JSON Canonicalization Scheme: object keys sorted by UTF-16 code
unit, no whitespace, strings escaped as ECMAScript's `JSON.stringify` escapes them, and numbers
written as ECMAScript writes them (`4`, not `4.0`; `1e-7`, not `1e-07`). NaN and Infinity have no
canonical form and MUST be refused. An integer beyond 2^53 is outside the range every implementation
can represent and SHOULD be carried as a string. `conformance/adr-vectors.json` holds the values an
implementation must reproduce.

`seal.canon` names the form the hash covers and is the only value defined here. A record without it
was sealed by Warrant before 0.9.0, over Python's `json.dumps` formatting with sorted keys, which
differs from RFC 8785 only in how some numbers are written. Verifiers SHOULD still accept such
records under that rule; an implementation that cannot reproduce it can verify exactly those whose
numbers the two forms write alike. A verifier MUST reject a `seal.canon` it does not know. The field
is outside the hashed body and is not signed: it selects how the hash is recomputed, and a record
re-labelled with the wrong form no longer matches its own hash.

A signing issuer then sets

```
seal.sealed_at = the time the issuer's store sealed the record (RFC 3339, UTC)
seal.key_id    = "ed25519:" || first 16 hex characters of SHA-256(raw public key)
seal.signature = base64( Ed25519.sign( "adr/0.2 seal\n" || seal.hash || "\n" || seal.sealed_at ) )
```

The signature is outside the hashed body, so a record can be verified with or without it. A record
signed without `sealed_at` (Warrant 0.7.0) signs `"adr/0.2 seal\n" || seal.hash` alone; verifiers MUST
accept both forms.

### 5.2 Keys and revocation

An issuer publishes its public keys as

```json
{"keys": [{"issuer": "demo-bank", "key_id": "ed25519:3f2a...", "alg": "Ed25519",
           "public_key": "<base64 raw 32 bytes>", "not_before": "2026-09-27T00:00:00Z",
           "revoked_at": null}]}
```

A signature by a key is valid only for records sealed (`seal.sealed_at`, or `timestamp` when absent)
at or after `not_before` and, if the key is revoked, before `revoked_at`. Validity follows the sealing
time, not the decision time: a record imported today describes a past decision but was signed today.

`sealed_at` is signed, so it cannot be moved without the key. It does not stop the holder of a key
backdating a record signed after the key was revoked; only a checkpoint co-signed by a witness before
the revocation (section 6) fixes which records existed by then.

### 5.3 What signing does and does not prove

A valid signature proves the record was sealed by the holder of the issuer's key and not changed
since. It does not prove the issuer did not rewrite its own history before anyone else saw it: the
issuer holds its own key. Level 3 exists for that.

## 6. Checkpoints, witnesses and anchoring

A **checkpoint** commits to a whole chain up to a size, as an RFC 9162 Merkle tree whose leaves are
the records' `seal.hash` values in sequence order:

```
leaf(h)    = SHA-256( 0x00 || h )
node(l, r) = SHA-256( 0x01 || l || r )
```

```json
{"spec": "adr/0.2", "kind": "checkpoint", "tenant": "demo-bank", "stream": "lending",
 "tree_size": 1200, "root": "<hex>", "head_hash": "<seal.hash of record 1200>",
 "issued_at": "...", "issuer_key_id": "ed25519:...", "signature": "<base64>",
 "cosignatures": [], "anchors": []}
```

The issuer signs `"adr/0.2 checkpoint\n" || canonical_json(checkpoint without signature,
cosignatures and anchors)`.

A **witness** holds no records. It keeps the last checkpoint it signed for each chain and, given a
new one, requires an RFC 9162 **consistency proof** that the new tree extends the old one. Only then
does it add `{witness, key_id, at, signature}` to `cosignatures`, signing the same message. An
issuer that rewrote history cannot produce that proof. An **inclusion proof** shows a single record
is in a checkpointed tree without revealing any other record.

**Anchoring** is optional. A checkpoint root MAY be submitted to OpenTimestamps calendars, which
commit it to Bitcoin. The anchor is a witness of last resort: blockchain as notary, not as ledger.

## 7. Conformance levels

| Level | A verifier confirms |
|---|---|
| **L1 Recorded** | Every record is schema-valid, correctly chained and signed by a key in the issuer's key set that was valid at the record's time. |
| **L2 Warranted** | L1, and every decision record carrying obligations has a verdict consistent with the admissibility rules, and every lifecycle path is legal. |
| **L3 Attested** | L2, and the chain head is covered by a checkpoint signed by the issuer and co-signed by at least one witness whose key is not the issuer's. |

A record with no signature is below L1. The reference verifier reports the level a chain reaches
and why it stops there.

## 8. Relationship to other standards

- An **A2A** message carries an ADR reference: `adr://<issuer>/<record_id>#<seal.hash>`.
- An **AP2** mandate or a **UPI** mandate is evidence of type `mandate`.
- A payment over UPI, cards or **x402** SHOULD be initiated only on a `warranted` record.
- An **ERC-8004** validation entry can point at an ADR's `seal.hash`.
- For payment agents, `actor.identity` SHOULD reference the payment system's own agent registry
  rather than a registry defined here.

## 9. What ADR does not claim

- Locating the step at which a decision went wrong is not assigning liability. The contract between
  the parties decides who pays.
- Admissibility of records as electronic evidence in court (in India, section 63 of the Bharatiya
  Sakshya Adhiniyam 2023) requires a legal opinion that this specification does not provide.
- Signing alone does not stop an issuer rewriting its own history (section 5.3).

## Changes within 0.2

- Warrant 0.7.1: the store signs `seal.sealed_at` with the hash, and key validity is judged at the
  sealing time (section 5). Found when the first imported records, of decisions made before the
  issuer's key existed, failed verification.

## Changes from 0.1

- Salted digests for sensitive evidence, the sidecar and erasure (section 4).
- Admissibility reason codes fixed and ordered (section 2).
- Checkpoints use RFC 9162 trees with consistency proofs, so a witness needs no records (section 6).
- OpenTimestamps anchoring as an optional Level 3 profile.
- Expressed as optional fields on Warrant schema v0, so existing records stay valid.
