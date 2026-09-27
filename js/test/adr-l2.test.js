// The Agent Decision Record's level 2 in JavaScript, end to end: obligations from a policy bundle,
// a signed upstream record cited, commit on a warrant, fail closed without one, and transitions.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Keyring, NotWarranted, SigningKey, Warrant, recordHash, validate } from "../src/index.js";
import { CelPolicyEngine, PolicyBundle, PolicyError } from "../src/policy.js";

const quiet = { info() {}, warn() {}, error() {} };
const CR09 = new URL("../../examples/adr/policies/CR-09.yaml", import.meta.url).pathname;
const partnerKey = SigningKey.fromPrivateBytes("partner-data", Buffer.alloc(32, 3));
const partnerKeys = new Keyring([partnerKey.public]);
const recent = () => new Date(Date.now() - 60_000).toISOString();

function sealAndSign(record, key) {
  const body = { ...record, sequence: 1 };
  const hash = recordHash(body, null);
  return { ...body, seal: { prev_hash: null, hash, ...key.signSeal(hash, new Date().toISOString()) } };
}

async function bank({ enforce = false } = {}) {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const policy = new CelPolicyEngine(await PolicyBundle.load(CR09, { logger: quiet }), { logger: quiet });
  const w = new Warrant("lending", { tenant: "demo-bank", store: sink, policy, enforce, agent: { name: "credit-agent", version: "3" }, spillDir: await mkdtemp(join(tmpdir(), "l2-")), logger: quiet });
  return { w, sink };
}

async function partnerRecord({ state = "committed" } = {}) {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const w = new Warrant("gst", { tenant: "partner-data", store: sink, agent: { name: "gst-agent", version: "1" }, spillDir: await mkdtemp(join(tmpdir(), "l2p-")), logger: quiet });
  await w.decide("gst.verify", { subject: "GSTIN:1" }, (d) => {
    d.obligation("G-1", { requires: "tool_call", providers: ["gstn"] });
    d.evidence("gstn_returns", { uri: "gstn://1", type: "tool_call", provider: "gstn", content: { band: "4-5 crore" }, retrievedAt: recent(), sensitive: true });
    if (state === "committed") d.commit("verified");
  });
  await w.flush();
  const { _blobs, ...record } = sink.records[0];
  return sealAndSign(record, partnerKey);
}

test("a warranted decision commits, and the record carries obligations, admissions, verdict and retention", async () => {
  const parent = await partnerRecord();
  assert.equal(parent.verdict.state, "committed");
  const { w, sink } = await bank();
  await w.decide("credit.msme.approve", { subject: "LN-1" }, (d) => {
    const verdict = d.check({ amount: 2_000_000, bureau_score: 742 });
    assert.deepEqual(verdict.obligations.map((o) => o.id), ["OB-1", "OB-2"]); // OB-3 only above 25,00,000
    assert.equal(verdict.enforce, true);
    d.evidence("bureau_pull", { uri: "cibil://1", type: "tool_call", provider: "cibil", content: { score: 742 }, retrievedAt: recent() });
    d.cite(parent, { keyring: partnerKeys });
    const state = d.warrant();
    assert.equal(state.state, "warranted");
    assert.equal(state.warranted, true);
    d.commit("approve");
  });
  await w.flush();
  const record = sink.records[0];
  validate(record);
  assert.equal(record.decision.status, "acted");
  assert.equal(record.verdict.state, "committed");
  assert.equal(record.verdict.decided_by, "policy:CR-09@2026.10");
  assert.deepEqual(record.verdict.met, ["OB-1", "OB-2"]);
  assert.deepEqual(record.verdict.history.map((h) => h.state), ["proposed", "warranted", "committed"]);
  assert.ok(record.evidence.filter((e) => e.obligation).every((e) => e.admission.status === "admitted"));
  assert.equal(record.parents[0].issuer, "partner-data");
  assert.equal(record.retention.class, "rbi-credit-8y");
  assert.ok(record.retention.retain_until > record.timestamp);
});

test("self-attested evidence fails closed, and the record says what was rejected", async () => {
  const { w, sink } = await bank();
  await assert.rejects(
    w.decide("credit.msme.approve", { subject: "LN-2" }, (d) => {
      d.check({ amount: 2_000_000, bureau_score: 742 });
      d.evidence("partner says ok", { uri: "msg://1", type: "record", provider: "self", content: { ok: true }, obligation: "OB-2" });
      const state = d.warrant();
      assert.deepEqual([...state.unmet], ["OB-1", "OB-2"]);
      assert.deepEqual(state.rejected.map((r) => [...r]), [["partner says ok", "self_attested"]]);
      d.commit("approve");
    }),
    (err) => err instanceof NotWarranted && err.state === "pending_evidence" && err.unmet.join() === "OB-1,OB-2",
  );
  await w.flush();
  const record = sink.records[0];
  validate(record);
  assert.equal(record.decision.status, "failed");
  assert.equal(record.evidence[0].admission.reason, "self_attested");
  assert.equal(record.verdict.state, "pending_evidence");
});

test("an enforcing policy blocks plain act(), and a client can enforce everywhere", async () => {
  const { w } = await bank();
  await assert.rejects(w.decide("credit.msme.approve", { subject: "LN-3" }, (d) => {
    d.check({ amount: 2_000_000, bureau_score: 742 });
    d.act("approve");
  }), NotWarranted);
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const strict = new Warrant("s", { store: sink, enforce: true, agent: { name: "a", version: "1" }, spillDir: await mkdtemp(join(tmpdir(), "l2s-")), logger: quiet });
  await assert.rejects(strict.decide("credit.approve", { subject: "x" }, (d) => {
    d.obligation("OB-1", { requires: "tool_call", providers: ["cibil"] });
    d.act("approve");
  }), NotWarranted);
});

test("a conditional obligation applies only when its condition holds, and a person warrants it later", async () => {
  const parent = await partnerRecord();
  const { w, sink } = await bank();
  let shown;
  await w.decide("credit.msme.approve", { subject: "LN-4" }, (d) => {
    d.check({ amount: 3_500_000, bureau_score: 742 });
    shown = d.evidence("bureau_pull", { uri: "cibil://1", type: "tool_call", provider: "cibil", content: {}, retrievedAt: recent() });
    d.cite(parent, { keyring: partnerKeys });
    const state = d.warrant();
    assert.deepEqual([...state.unmet], ["OB-3"]);
  });
  await w.flush();
  const decision = sink.records[0];
  assert.equal(decision.verdict.state, "pending_evidence");
  const id = decision.record_id;

  assert.throws(() => w.transition(id, "warranted", { decidedBy: "human:x", fromState: "pending_evidence" }), /pass decision/);
  assert.throws(() => w.transition(id, "warranted", { decidedBy: "human:x", fromState: "pending_evidence", decision, reviewer: "x", shown: ["f".repeat(64)] }), /material_not_linked/);
  assert.throws(() => w.transition(id, "committed", { decidedBy: "agent:sanction", fromState: "pending_evidence" }), /illegal transition pending_evidence -> committed/);
  assert.throws(() => w.transition(id, "warranted", { decidedBy: "human:x", fromState: "escalated" }), /named reviewer/);
  assert.throws(() => w.transition(id, "warranted", { decidedBy: "human:x" }), /fromState/);

  w.transition(id, "warranted", { decidedBy: "human:officer", fromState: "pending_evidence", decision, reviewer: "officer", shown: [shown] });
  w.transition(id, "committed", { decidedBy: "agent:sanction", fromState: "warranted" });
  await w.flush();
  const [, warranted, committed] = sink.records;
  for (const r of [warranted, committed]) validate(r);
  assert.equal(warranted.record_type, "transition");
  assert.deepEqual(warranted.human.shown, [shown]);
  assert.equal(committed.verdict.from_state, "warranted");
});

test("a transition cannot add missing evidence", async () => {
  const { w, sink } = await bank();
  await w.decide("credit.msme.approve", { subject: "LN-5" }, (d) => { d.check({ amount: 2_000_000, bureau_score: 742 }); });
  await w.flush();
  const decision = sink.records[0];
  assert.throws(() => w.transition(decision.record_id, "warranted", { decidedBy: "human:x", fromState: "pending_evidence", decision, reviewer: "x", shown: [] }), /need evidence/);
});

test("malformed obligations are load errors, as in Python", async () => {
  const dir = await mkdtemp(join(tmpdir(), "l2pol-"));
  const base = "policy_id: P\nversion: '1'\nclasses: [c.x]\nclauses:\n  - {id: '1', when: 'true', result: allow}\n";
  const cases = [
    ["obligations:\n  - {id: A, requires: tool_call, providers: [self]}\n", /cannot be a qualified provider/],
    ["obligations:\n  - {id: A, requires: tool_call, max_age: soon}\n", /not a duration/],
    ["obligations:\n  - {id: A, requires: tool_call, clause: '9'}\n", /not a clause/],
    ["obligations:\n  - {id: A, requires: telepathy}\n", /requires must be/],
    ["obligations:\n  - {id: A, requires: tool_call}\n  - {id: A, requires: document}\n", /duplicate obligation/],
    ["enforce: yes-please\n", /'enforce' must be true or false/],
  ];
  for (const [i, [extra, message]] of cases.entries()) {
    const file = join(dir, `p${i}.yaml`);
    await writeFile(file, base + extra);
    await assert.rejects(PolicyBundle.load(file, { logger: quiet }), (err) => err instanceof PolicyError && message.test(err.message));
  }
});

test("an obligation whose condition cannot evaluate applies", async () => {
  const dir = await mkdtemp(join(tmpdir(), "l2when-"));
  const file = join(dir, "p.yaml");
  await writeFile(file, "policy_id: P\nversion: '1'\nclasses: [c.x]\nclauses:\n  - {id: '1', when: 'true', result: allow}\nobligations:\n  - {id: A, requires: document, when: 'missing_input > 5'}\n");
  const engine = new CelPolicyEngine(await PolicyBundle.load(file, { logger: quiet }), { logger: quiet });
  assert.deepEqual(engine.evaluate("c.x", {}).obligations.map((o) => o.id), ["A"]);
});
