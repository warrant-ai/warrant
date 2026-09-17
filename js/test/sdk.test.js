import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Redactor, ValidationError, Verdict, Warrant, canonicalJson, contentHash, currentDecision, ulid, validate } from "../src/index.js";

const quiet = { info() {}, warn() {}, error() {} };

async function client(options = {}) {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const spillDir = await mkdtemp(join(tmpdir(), "warrant-sdk-"));
  const w = new Warrant("lending", {
    tenant: "demo-bank", store: sink, agent: { name: "credit-underwriter", version: "2.3.1", instance: "test" },
    onBehalfOf: "branch:jayanagar", currency: "INR", spillDir, flushIntervalMs: 5, logger: quiet, ...options,
  });
  return { w, sink };
}

const policy = {
  evaluate(decisionClass, inputs) {
    assert.equal(decisionClass, "credit.approve");
    return inputs.amount <= 500000
      ? new Verdict("allow", { policyId: "CR-07", policyVersion: "2026.3", clause: "4.2" })
      : new Verdict("deny", { policyId: "CR-07", policyVersion: "2026.3", clause: "4.1", reason: "over limit" });
  },
};

test("an acted decision is recorded with mandate, evidence and cost", async () => {
  const { w, sink } = await client({ policy });
  const returned = await w.decide("credit.approve", { subject: "LN-1", alternatives: ["refer"] }, async (d) => {
    assert.equal(currentDecision(), d);
    const verdict = d.check({ amount: 450000 });
    assert.equal(verdict.allowed, true);
    d.evidence("bureau_pull", { uri: "cibil://req/1", content: { score: 748 }, excerpt: "score 748" });
    d.modelCall("anthropic", "claude-sonnet-5", { tokensIn: 1200, tokensOut: 300, amount: 3.5 });
    d.toolCall("kyc_lookup", { content: "ok", amount: 0.34 });
    d.act("approve", { summary: "Within limit.", costCentre: "retail-lending" });
    return "done";
  });
  assert.equal(returned, "done");
  assert.equal(currentDecision(), undefined);
  assert.equal(await w.flush(), true);
  const [record] = sink.records;
  validate(record);
  assert.match(record.record_id, /^[0-9A-HJKMNP-TV-Z]{26}$/);
  assert.match(record.timestamp, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  assert.equal("seal" in record, false);
  assert.deepEqual(
    { ...record, record_id: "", timestamp: "", evidence: record.evidence.map((e) => ({ ...e, content_hash: "" })) },
    {
      record_id: "", record_type: "decision", tenant: "demo-bank", stream: "lending", timestamp: "", schema_version: "0", origin: "live",
      actor: { name: "credit-underwriter", version: "2.3.1", instance: "test", on_behalf_of: "branch:jayanagar" },
      decision: { class: "credit.approve", action: "approve", subject: "LN-1", status: "acted", summary: "Within limit.", alternatives: ["refer"] },
      mandate: { result: "allow", policy_id: "CR-07", policy_version: "2026.3", clause: "4.2" },
      evidence: [
        { name: "bureau_pull", type: "other", uri: "cibil://req/1", content_hash: "", excerpt: "score 748" },
        { name: "anthropic/claude-sonnet-5", type: "model_call", uri: "model://anthropic/claude-sonnet-5", content_hash: "" },
        { name: "kyc_lookup", type: "tool_call", uri: "tool://kyc_lookup", content_hash: "" },
      ],
      human: { required: false },
      cost: {
        amount: 3.84, currency: "INR", cost_centre: "retail-lending",
        breakdown: [
          { kind: "model_call", amount: 3.5, provider: "anthropic", model: "claude-sonnet-5", tokens_in: 1200, tokens_out: 300 },
          { kind: "tool_call", amount: 0.34, provider: "kyc_lookup" },
        ],
      },
      outcome: { status: "pending" },
    },
  );
  assert.equal(record.evidence[0].content_hash, contentHash({ score: 748 }));
  await w.close();
});

test("a denied decision that does not act is withheld, and unchecked is never allowed", async () => {
  const { w, sink } = await client({ policy, captureInputs: true });
  await w.decide("credit.approve", { subject: "LN-2", onBehalfOf: "branch:hubli" }, (d) => {
    if (!d.check({ amount: 900000 }).allowed) d.requireHuman({ note: "over limit" });
  });
  const bare = await client();
  await bare.w.decide("credit.approve", { subject: "LN-3" }, (d) => {
    assert.equal(d.check({ amount: 1 }).allowed, false);
  });
  await w.flush();
  await bare.w.flush();
  const [denied] = sink.records;
  assert.deepEqual(denied.decision, { class: "credit.approve", action: "none", subject: "LN-2", status: "withheld", summary: "withheld: mandate result was deny", inputs: { amount: 900000 } });
  assert.deepEqual(denied.mandate, { result: "deny", policy_id: "CR-07", policy_version: "2026.3", clause: "4.1", reason: "over limit" });
  assert.deepEqual(denied.human, { required: true, note: "over limit" });
  assert.equal(denied.actor.on_behalf_of, "branch:hubli");
  assert.deepEqual(bare.sink.records[0].mandate, { result: "unchecked", reason: "no policy engine configured" });
  assert.throws(() => new Verdict("maybe"), RangeError);
});

test("a throwing scope records the error class only and rethrows", async () => {
  const { w, sink } = await client();
  class BureauTimeout extends Error {}
  await assert.rejects(
    w.decide("credit.approve", { subject: "LN-4" }, async () => {
      throw new BureauTimeout("PAN ABCDE1234F not found for Asha Rao");
    }),
    BureauTimeout,
  );
  await w.flush();
  const [record] = sink.records;
  assert.equal(record.decision.status, "failed");
  assert.equal(record.decision.summary, "BureauTimeout raised before the action completed");
  assert.equal(JSON.stringify(record).includes("ABCDE1234F"), false);
});

test("misuse is rejected at the boundary", async () => {
  const { w, sink } = await client();
  await assert.rejects(w.decide("Credit Approve", { subject: "x" }, () => {}), /decision class must look like/);
  await assert.rejects(w.decide("credit.approve", {}, () => {}), /subject must be a non-empty string/);
  await assert.rejects(w.decide("credit.approve", { subject: "x" }), /needs a function/);
  let escaped;
  await w.decide("credit.approve", { subject: "LN-5" }, (d) => {
    escaped = d;
    assert.throws(() => d.evidence("e", { uri: "x://y" }), /needs either content/);
    assert.throws(() => d.evidence("e", { uri: "x://y", contentHash: "abc" }), /64 lowercase hex/);
    assert.throws(() => d.evidence("e", { uri: "x://y", content: "c", type: "pdf" }), RangeError);
    assert.throws(() => d.cost(-1), RangeError);
    assert.throws(() => d.cost(1, { tokensIn: 1.5 }), RangeError);
    assert.throws(() => d.setInputs({ n: 1n }), /JSON-serialisable/);
    d.act("approve");
    assert.throws(() => d.act("approve"), /already called/);
  });
  assert.throws(() => escaped.cost(1), /is closed/);
  assert.throws(() => new Warrant("lending", { agent: { name: "a", version: "1" }, logger: quiet }), /store must be a collector URL/);
  assert.throws(() => new Warrant("lending", { store: { write() {} }, currency: "rupees", logger: quiet }), RangeError);
  await w.flush();
  assert.equal(sink.records.length, 1);
});

test("outcomes and human verdicts are linked records", async () => {
  const { w, sink } = await client();
  let id;
  await w.decide("credit.approve", { subject: "LN-6" }, (d) => {
    id = d.recordId;
    d.act("approve");
  });
  const outcomeId = w.outcome({ label: "default", decisionRecordId: id, observedAt: new Date("2026-12-15T00:00:00Z"), score: 0.2, source: "lms://x" });
  w.humanVerdict({ reviewer: "asha", verdict: "reject", decisionRecordId: id, note: "Income not evidenced." });
  assert.throws(() => w.outcome({ label: "default", decisionRecordId: "LN-6" }), /record id of the decision/);
  assert.throws(() => w.outcome({ decisionRecordId: id }), /label/);
  assert.throws(() => w.humanVerdict({ reviewer: "asha", verdict: "maybe", decisionRecordId: id }), RangeError);
  await w.flush();
  const [, outcome, verdict] = sink.records;
  sink.records.forEach(validate);
  assert.equal(outcome.record_id, outcomeId);
  assert.deepEqual(outcome.references, { decision_record_id: id });
  assert.deepEqual(outcome.outcome, { status: "observed", label: "default", observed_at: "2026-12-15T00:00:00.000Z", score: 0.2, source: "lms://x" });
  assert.deepEqual({ ...verdict.human, at: "" }, { required: true, reviewer: "asha", verdict: "reject", at: "", note: "Income not evidenced." });
});

test("redaction touches free text and named fields, never structure", async () => {
  const redact = new Redactor({ patterns: [/[A-Z]{5}\d{4}[A-Z]/, "\\d{10}"], fields: ["actor.on_behalf_of", "decision.inputs"], replacement: "[$&]" });
  const { w, sink } = await client({ redact, captureInputs: true });
  await w.decide("credit.approve", { subject: "ABCDE1234F", alternatives: ["call 9876543210"] }, (d) => {
    d.check({ pan: "ABCDE1234F" });
    d.evidence("pan", { uri: "kyc://ABCDE1234F", content: "x", excerpt: "PAN ABCDE1234F, phone 9876543210 and 9123456780" });
    d.act("approve", { summary: "PAN ABCDE1234F verified" });
  });
  await w.flush();
  const [record] = sink.records;
  assert.equal(record.decision.summary, "PAN [$&] verified");
  assert.equal(record.evidence[0].excerpt, "PAN [$&], phone [$&] and [$&]");
  assert.deepEqual(record.decision.alternatives, ["call [$&]"]);
  assert.equal(record.decision.subject, "ABCDE1234F");
  assert.equal(record.evidence[0].uri, "kyc://ABCDE1234F");
  assert.equal(record.actor.on_behalf_of, "[$&]");
  assert.equal("inputs" in record.decision, false);
  assert.throws(() => new Redactor({ fields: [".bad"] }), /invalid field path/);
});

test("an invalid record is refused, not sent", async () => {
  const { w, sink } = await client({ tenant: "" });
  w.tenant = "";
  await assert.rejects(w.decide("credit.approve", { subject: "LN-7" }, (d) => d.act("approve")), ValidationError);
  await w.flush();
  assert.equal(sink.records.length, 0);
});

test("hashing matches the Python SDK", () => {
  assert.equal(contentHash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert.equal(contentHash({ score: 748, name: "Asha", tags: ["a", "b"], nested: { z: null, a: true }, ratio: 0.38 }), "02dd4a26791595bd41d7c375fd600655338094cf947b6cc4ec9276023b68efea");
  assert.equal(contentHash("ನಮಸ್ಕಾರ"), "0280f7d60f5e2227d52d1deeb9df4f0cce320b7af830c459624b11446c66bb78");
  assert.equal(contentHash({ city: "ಬೆಂಗಳೂರು" }), "3437fc23f329fef6707f5d74fc804adf8bfc2bd013b88f01fbd28eef94c1e4a5");
  assert.equal(contentHash(new Uint8Array([0, 1])), "b413f47d13ee2fe6c845b2ee141af81de858df4ec549a58b7970bb96645bc8d2");
  assert.equal(canonicalJson({ b: 1, a: [{ d: undefined, c: "x" }] }), '{"a":[{"c":"x"}],"b":1}');
  assert.throws(() => contentHash(() => {}), TypeError);
});

test("ulids sort by time", () => {
  const early = ulid(1_700_000_000_000);
  const late = ulid(1_800_000_000_000);
  assert.match(early, /^[0-9A-HJKMNP-TV-Z]{26}$/);
  assert.ok(early < late);
  assert.notEqual(ulid(1_700_000_000_000), early);
  assert.throws(() => ulid(-1), RangeError);
});
