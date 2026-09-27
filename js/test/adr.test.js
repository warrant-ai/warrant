import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CHECKPOINT_CONTEXT, CitationError, Keyring, PublicKey, SEAL_CONTEXT, SigningKey, Warrant,
  canonicalJson, consistencyProof, contentHash, inclusionProof, keyIdFor, merkleRoot, recordHash,
  saltedHash, validate, verifyConsistency, verifyInclusion, verifySeal,
} from "../src/index.js";

const vectors = JSON.parse(readFileSync(new URL("../../conformance/adr-vectors.json", import.meta.url), "utf8"));
const quiet = { info() {}, warn() {}, error() {} };
const vectorKey = SigningKey.fromPrivateBytes(vectors.key.issuer, Buffer.from(vectors.key.private_key_hex, "hex"));

// -- the shared vectors: Python produced them, JavaScript must reproduce them byte for byte ----

test("key id and public key match the vectors", () => {
  assert.equal(vectorKey.keyId, vectors.key.key_id);
  assert.equal(vectorKey.public.raw.toString("base64"), vectors.key.public_key_b64);
  assert.equal(keyIdFor(vectorKey.public.raw), vectors.key.key_id);
});

test("plain and salted digests match the vectors", () => {
  for (const v of vectors.salted) {
    assert.equal(contentHash(v.content), v.plain);
    assert.equal(saltedHash(v.content, Buffer.from(v.salt_hex, "hex")), v.salted);
    assert.notEqual(v.salted, v.plain);
  }
  assert.throws(() => saltedHash("x", Buffer.alloc(8)), RangeError);
});

test("the seal hash, message and exact signature match the vectors", () => {
  const { record, hash, message, signature } = vectors.seal;
  assert.equal(recordHash(record, null), hash);
  assert.equal(SEAL_CONTEXT + hash, message);
  assert.equal(vectorKey.sign(message), signature);
  assert.deepEqual(vectorKey.signSeal(hash), { key_id: vectors.key.key_id, signature });
});

test("the checkpoint message matches the vectors", () => {
  const { body, message } = vectors.checkpoint_message;
  assert.equal(CHECKPOINT_CONTEXT + canonicalJson(body), message);
});

test("every Merkle root, inclusion and consistency proof matches and verifies", () => {
  const { entries, roots, inclusion, consistency } = vectors.merkle;
  for (const [n, root] of Object.entries(roots)) assert.equal(merkleRoot(entries.slice(0, Number(n))), root);
  for (const v of inclusion) {
    const leaves = entries.slice(0, v.size);
    assert.deepEqual(inclusionProof(v.index, leaves), v.proof);
    assert.equal(verifyInclusion(entries[v.index], v.index, v.size, v.proof, roots[String(v.size)]), true);
    if (v.size > 1) assert.equal(verifyInclusion(entries[(v.index + 1) % v.size], v.index, v.size, v.proof, roots[String(v.size)]), false);
  }
  for (const v of consistency) {
    const leaves = entries.slice(0, v.to);
    assert.deepEqual(consistencyProof(v.from, leaves), v.proof);
    assert.equal(verifyConsistency(v.from, v.to, roots[String(v.from)], roots[String(v.to)], v.proof), true);
    if (v.from < v.to) {
      const rewritten = [...leaves];
      rewritten[v.from - 1] = "00".repeat(32);
      assert.equal(verifyConsistency(v.from, v.to, roots[String(v.from)], merkleRoot(rewritten), consistencyProof(v.from, rewritten)), false);
    }
  }
  assert.throws(() => inclusionProof(5, entries.slice(0, 5)), RangeError);
});

// -- verifying seals -----------------------------------------------------------------------

function signed(record, key) {
  const body = { ...record, sequence: 1 };
  const hash = recordHash(body, null);
  return { ...body, seal: { prev_hash: null, hash, ...key.signSeal(hash) } };
}

test("verifySeal says why a signature fails", () => {
  const record = signed(vectors.seal.record, vectorKey);
  const ring = new Keyring([vectorKey.public]);
  assert.deepEqual(verifySeal(record, ring), { ok: true, issuer: "vector-issuer" });
  const { signature, ...unsigned } = record.seal;
  assert.deepEqual(verifySeal({ ...record, seal: unsigned }, ring), { ok: false, reason: "unsigned" });
  const other = SigningKey.fromPrivateBytes("other", Buffer.alloc(32, 7));
  assert.match(verifySeal(record, new Keyring([other.public])).reason, /unknown key/);
  const revoked = new PublicKey(vectorKey.issuer, vectorKey.public.raw, { revokedAt: "2026-01-01T00:00:00Z" });
  assert.match(verifySeal(record, new Keyring([revoked])).reason, /revoked/);
  assert.match(verifySeal({ ...record, seal: { ...record.seal, hash: "0".repeat(64) } }, ring).reason, /does not verify/);
});

test("a key set with a private key, or a mismatched key id, is refused; revocations stick", () => {
  const entry = vectorKey.public.toEntry();
  assert.throws(() => Keyring.fromKeySets({ keys: [{ ...entry, private_key: "x" }] }), /private key/);
  assert.throws(() => Keyring.fromKeySets({ keys: [{ ...entry, key_id: "ed25519:0000000000000000" }] }), /does not match/);
  const ring = Keyring.fromKeySets({ keys: [{ ...entry, revoked_at: "2026-01-01T00:00:00Z" }] }, { keys: [entry] });
  assert.equal(ring.get(vectorKey.keyId).revokedAt, "2026-01-01T00:00:00Z");
});

// -- the record path -----------------------------------------------------------------------

async function client(options = {}) {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const spillDir = await mkdtemp(join(tmpdir(), "warrant-adr-"));
  const w = new Warrant("lending", {
    tenant: "demo-bank", store: sink, currency: "INR", spillDir, flushIntervalMs: 5, logger: quiet,
    agent: { name: "credit-agent", version: "3", model: "laya@55cf4c4ebb4e/multilingual", runtime: "temporal", identity: ["npci-agent-registry", "AGT-1"] },
    ...options,
  });
  return { w, sink };
}

function body(record) {
  const { _blobs, ...rest } = record;
  return rest;
}

test("sensitive evidence is salted, its salt rides in the sidecar, and an excerpt or bare hash is refused", async () => {
  const { w, sink } = await client();
  let digest;
  await w.decide("credit.approve", { subject: "LN-1" }, (d) => {
    digest = d.evidence("pan", { uri: "kyc://1", type: "document", provider: "nsdl", content: "ABCDE1234F", sensitive: true, obligation: "OB-1" });
    assert.equal(d.saltFor(digest).length, 64);
    assert.equal(saltedHash("ABCDE1234F", Buffer.from(d.saltFor(digest), "hex")), digest);
    assert.notEqual(digest, contentHash("ABCDE1234F"));
    assert.throws(() => d.evidence("pan", { uri: "x://1", content: "ABCDE1234F", excerpt: "ABCDE1234F", sensitive: true }), /excerpt/);
    assert.throws(() => d.evidence("pan", { uri: "x://1", contentHash: "a".repeat(64), sensitive: true }), /bare hash/);
  });
  await w.flush();
  const [record] = sink.records;
  validate(body(record));
  const item = record.evidence[0];
  assert.deepEqual({ salted: item.salted, provider: item.provider, obligation: item.obligation }, { salted: true, provider: "nsdl", obligation: "OB-1" });
  assert.equal(record._blobs[digest].salt.length, 64);
  assert.equal(JSON.stringify(body(record)).includes(record._blobs[digest].salt), false, "the salt never reaches the record");
  await w.close();
});

test("claims, retention and agent identity reach the record and validate", async () => {
  const { w, sink } = await client();
  await w.decide("credit.approve", { subject: "LN-2" }, (d) => {
    d.claim("gst_turnover_inr", 42000000);
    d.claim("filings_verified");
    d.retention("rbi-credit-8y", { retainUntil: "2034-09-27T00:00:00Z", legalHold: true });
    assert.throws(() => d.claim(""), TypeError);
  });
  await w.flush();
  const [record] = sink.records;
  validate(body(record));
  assert.deepEqual(record.decision.claims, [{ claim: "gst_turnover_inr", value: 42000000 }, { claim: "filings_verified" }]);
  assert.deepEqual(record.retention, { class: "rbi-credit-8y", retain_until: "2034-09-27T00:00:00Z", legal_hold: true });
  assert.deepEqual(record.actor.identity, { registry: "npci-agent-registry", id: "AGT-1" });
  assert.equal(record.actor.model, "laya@55cf4c4ebb4e/multilingual");
  assert.equal(record.actor.runtime, "temporal");
  await w.close();
});

test("cite writes a parent link and record evidence, checks the signature, and refuses an altered parent", async () => {
  const partner = SigningKey.fromPrivateBytes("partner-data", Buffer.alloc(32, 3));
  const parent = signed({ ...vectors.seal.record, tenant: "partner-data", verdict: { state: "committed" } }, partner);
  const ring = new Keyring([partner.public]);
  const { w, sink } = await client();
  await w.decide("credit.approve", { subject: "LN-3" }, (d) => {
    const hash = d.cite(parent, { keyring: ring, obligation: "OB-2" });
    assert.equal(hash, parent.seal.hash);
    assert.throws(() => d.cite({ ...parent, decision: { ...parent.decision, summary: "edited" } }), CitationError);
    assert.throws(() => d.cite(parent, { keyring: new Keyring([vectorKey.public]) }), /unknown key/);
    assert.throws(() => d.cite({ record_id: parent.record_id }), /not sealed/);
  });
  await w.flush();
  const [record] = sink.records;
  validate(body(record));
  assert.deepEqual(record.parents, [{ record_id: parent.record_id, hash: parent.seal.hash, issuer: "partner-data", key_id: partner.keyId, state: "committed" }]);
  const item = record.evidence[0];
  assert.equal(item.type, "record");
  assert.equal(item.provider, "partner-data");
  assert.equal(item.obligation, "OB-2");
  assert.equal(item.uri, `adr://partner-data/${parent.record_id}#${parent.seal.hash}`);
  assert.deepEqual(item.parent, { record_id: parent.record_id, hash: parent.seal.hash, issuer: "partner-data" });
  assert.equal("claims" in record.decision, false, "a parent's claims are never copied");
  await w.close();
});
