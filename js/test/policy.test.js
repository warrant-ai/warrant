import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Warrant } from "../src/index.js";
import { CelPolicyEngine, PolicyBundle, PolicyError, lintClause, runPolicyTests } from "../src/policy.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..");
const quiet = { info() {}, warn() {}, error() {} };
// The CEL engine needs Node.js 20.19 or newer; the rest of the SDK still runs on 18.
const skip = Number(process.versions.node.split(".")[0]) < 20 ? "policy bundles need Node.js 20.19+" : false;

async function bundleOf(policy, name = "p.json") {
  const dir = await mkdtemp(join(tmpdir(), "warrant-policy-"));
  await writeFile(join(dir, name), typeof policy === "string" ? policy : JSON.stringify(policy));
  return PolicyBundle.load(dir, { logger: quiet });
}

const base = { policy_id: "T-1", version: "1", classes: ["t.run"], clauses: [{ id: "a", when: "n > 1", result: "allow" }] };

test("shared conformance cases evaluate as every SDK must", { skip }, async () => {
  const { cases, divergent } = JSON.parse(await readFile(join(ROOT, "conformance", "policy-cases.json"), "utf8"));
  assert.ok(cases.length >= 30);
  for (const c of cases) {
    const bundle = await bundleOf({ ...base, fail_mode: "escalate", default: "deny", clauses: [{ id: "x", when: c.when, result: "allow" }] });
    const verdict = new CelPolicyEngine(bundle, { logger: quiet }).evaluate("t.run", c.inputs);
    const got = verdict.flagged ? "error" : verdict.result === "allow";
    assert.equal(got, c.expect, `${c.name}: ${c.when} -> ${verdict.reason}`);
  }
  for (const c of divergent) {
    const bundle = await bundleOf({ ...base, fail_mode: "escalate", clauses: [{ id: "x", when: c.when, result: "allow" }] });
    const verdict = new CelPolicyEngine(bundle, { logger: quiet }).evaluate("t.run", c.inputs);
    assert.equal(verdict.flagged ? "error" : verdict.result === "allow", c.javascript, `pinned JavaScript behaviour changed: ${c.name}`);
    assert.ok(lintClause(c.when).length > 0, `the linter should warn about: ${c.when}`);
    assert.deepEqual(lintClause(c.write_instead), []);
  }
});

test("the example bundle loads from YAML and its embedded tests pass", { skip }, async () => {
  const bundle = await PolicyBundle.load(join(ROOT, "examples", "policies"), { logger: quiet });
  assert.deepEqual(bundle.policies.map((p) => p.policyId).sort(), ["COL-02", "CR-07"]);
  const results = runPolicyTests(bundle);
  assert.ok(results.length >= 3);
  assert.deepEqual(results.filter((r) => !r.passed), []);
  const verdict = new CelPolicyEngine(bundle, { logger: quiet }).evaluate("credit.approve", { amount: 450000, bureau_score: 748, foir: 0.38 });
  assert.deepEqual({ ...verdict }, { result: "allow", policyId: "CR-07", policyVersion: "2026.3", clause: "4.2", reason: verdict.reason, flagged: false });
  assert.match(verdict.reason, /^Auto-approve/);
});

test("fail modes apply only when a clause cannot be evaluated", { skip }, async () => {
  for (const [failMode, result] of [["closed", "deny"], ["open", "allow"], ["escalate", "escalate"]]) {
    const warnings = [];
    const engine = new CelPolicyEngine(await bundleOf({ ...base, fail_mode: failMode, default: "escalate" }), { logger: { ...quiet, warn: (m) => warnings.push(m) } });
    const failed = engine.evaluate("t.run", {});
    assert.equal(failed.result, result);
    assert.equal(failed.flagged, true);
    assert.match(failed.reason, new RegExp(`^fail-${failMode}: clause a: `));
    assert.equal(warnings.length, 1);
    const unmatched = engine.evaluate("t.run", { n: 0 });
    assert.deepEqual([unmatched.result, unmatched.flagged, unmatched.reason, unmatched.clause], ["escalate", false, "no clause matched", undefined]);
  }
  const engine = new CelPolicyEngine(await bundleOf(base), { logger: quiet });
  assert.equal(engine.evaluate("t.run", { n: 1n }).flagged, true, "inputs that are not JSON fail the policy, they do not throw");
  assert.equal(engine.evaluate("t.other", { n: 5 }).result, "unchecked");
  assert.throws(() => new CelPolicyEngine({}), TypeError);
});

test("clauses run in order and class patterns resolve exact first, then longest prefix", { skip }, async () => {
  const dir = await mkdtemp(join(tmpdir(), "warrant-policy-"));
  await writeFile(join(dir, "a.json"), JSON.stringify({ ...base, policy_id: "EXACT", classes: ["credit.approve"], clauses: [{ id: "1", when: "n > 10", result: "deny" }, { id: "2", when: "n > 1", result: "allow" }] }));
  await writeFile(join(dir, "b.yaml"), "policy_id: WIDE\nversion: 2\nclasses: ['credit.*']\nclauses:\n  - {id: w, when: 'true', result: escalate}\n");
  await writeFile(join(dir, "c.yml"), "policy_id: DEEP\nversion: '3'\nclasses: ['credit.limit.*']\nclauses:\n  - {id: d, when: 'true', result: deny}\n");
  const engine = new CelPolicyEngine(await PolicyBundle.load(dir, { logger: quiet }), { logger: quiet });
  assert.equal(engine.evaluate("credit.approve", { n: 50 }).clause, "1");
  assert.equal(engine.evaluate("credit.approve", { n: 5 }).clause, "2");
  assert.equal(engine.evaluate("credit.decline", {}).policyId, "WIDE");
  assert.equal(engine.evaluate("credit.limit.raise", {}).policyId, "DEEP");
  assert.equal(engine.evaluate("credit", {}).result, "unchecked");
  assert.equal(engine.bundle.policyFor("credit.decline").version, "2");
});

test("malformed bundles are refused with the file and the reason", { skip }, async () => {
  const bad = [
    [{ ...base, policy_id: "" }, /'policy_id' must be a non-empty string/],
    [{ ...base, classes: ["Credit Approve"] }, /invalid class pattern/],
    [{ ...base, fail_mode: "ajar" }, /fail_mode must be one of/],
    [{ ...base, default: "unchecked" }, /default must be one of/],
    [{ ...base, clauses: [] }, /'clauses' must be a non-empty list/],
    [{ ...base, clauses: [{ id: "a", when: "n >", result: "allow" }] }, /clause a: CEL parse error/],
    [{ ...base, clauses: [{ id: "a", when: "true", result: "allow" }, { id: "a", when: "true", result: "deny" }] }, /duplicate clause id/],
    [{ ...base, clauses: [{ id: "a", when: "true", result: "unchecked" }] }, /result must be one of/],
    [{ ...base, tests: [{ inputs: {}, expect: "perhaps" }] }, /expect must be one of/],
    ["{not json", /p\.json: cannot parse/],
  ];
  for (const [policy, message] of bad) {
    await assert.rejects(bundleOf(policy), (err) => err instanceof PolicyError && message.test(err.message), String(message));
  }
  await assert.rejects(PolicyBundle.load("/no/such/bundle"), /policy bundle not found/);
  await assert.rejects(PolicyBundle.load(await mkdtemp(join(tmpdir(), "warrant-empty-"))), /no policy files/);
  const dir = await mkdtemp(join(tmpdir(), "warrant-policy-"));
  await writeFile(join(dir, "a.json"), JSON.stringify(base));
  await writeFile(join(dir, "b.json"), JSON.stringify({ ...base, policy_id: "T-2" }));
  await assert.rejects(PolicyBundle.load(dir, { logger: quiet }), /claimed by both T-1 \(always\) and T-2 \(always\); give each an effective_from/);
});

test("a failing embedded test is reported with what happened instead", { skip }, async () => {
  const bundle = await bundleOf({ ...base, tests: [{ name: "small n", inputs: { n: 0 }, expect: "allow", clause: "a" }, { inputs: { n: 5 }, expect: "allow" }] });
  const [failed, passed] = runPolicyTests(bundle);
  assert.deepEqual(failed, { policyId: "T-1", name: "small n", passed: false, expected: "allow", got: "deny", detail: "expected allow via clause a, got deny (no clause matched)" });
  assert.deepEqual([passed.name, passed.passed], ["test #2", true]);
});

test("the linter warns at load about comparisons and divisions that are not portable", { skip }, async () => {
  assert.deepEqual(lintClause("amount <= 500000 && double(foir) <= 0.45 && rate(x) > 0.5 && double(a) / double(b) < 1.0"), []);
  assert.equal(lintClause("0.45 >= applicant.foir").length, 1);
  assert.match(lintClause("emi / income <= 0.45")[0], /double\(emi\) \/ double\(income\)/);
  const warnings = [];
  const dir = await mkdtemp(join(tmpdir(), "warrant-policy-"));
  await writeFile(join(dir, "p.json"), JSON.stringify({ ...base, clauses: [{ id: "a", when: "foir <= 0.45", result: "allow" }] }));
  await PolicyBundle.load(dir, { logger: { ...quiet, warn: (m) => warnings.push(m) } });
  assert.deepEqual(warnings, ["warrant policy T-1 clause a compares foir with a decimal literal; a whole-number input cannot be evaluated by every engine, write double(foir)"]);
});

test("check() records the bundle's verdict on the decision", { skip }, async () => {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const policy = new CelPolicyEngine(await PolicyBundle.load(join(ROOT, "examples", "policies"), { logger: quiet }), { logger: quiet });
  const w = new Warrant("lending", { store: sink, policy, agent: { name: "a", version: "1" }, spillDir: await mkdtemp(join(tmpdir(), "warrant-policy-")), flushIntervalMs: 5, logger: quiet });
  await w.decide("credit.approve", { subject: "LN-1" }, (d) => { if (d.check({ amount: 900000, bureau_score: 790, foir: 0.3 }).allowed) d.act("approve"); });
  await w.decide("credit.approve", { subject: "LN-2" }, (d) => { d.check({ amount: 100000 }); });
  await w.flush();
  await w.close();
  assert.deepEqual(sink.records[0].mandate, { result: "escalate", policy_id: "CR-07", policy_version: "2026.3", clause: "4.3", reason: "Refer tickets above 5,00,000 to a credit officer" });
  assert.equal(sink.records[0].decision.status, "withheld");
  assert.equal(sink.records[1].mandate.result, "deny");
  assert.equal(sink.records[1].mandate.flagged, true);
});

const v1 = { ...base, version: "2026.1", effective_to: "2026-10-01" };
const v2 = { ...base, version: "2026.2", effective_from: "2026-10-01", clauses: [{ id: "b", when: "n > 100", result: "allow" }] };

async function datedBundle(...policies) {
  const dir = await mkdtemp(join(tmpdir(), "warrant-policy-"));
  for (const [i, policy] of policies.entries()) await writeFile(join(dir, `${i}.json`), JSON.stringify(policy));
  return PolicyBundle.load(dir, { logger: quiet });
}

test("a decision is judged by the policy version in force at its own timestamp", { skip }, async () => {
  const bundle = await datedBundle(v1, v2);
  const engine = new CelPolicyEngine(bundle, { logger: quiet });
  assert.equal(bundle.policyFor("t.run", "2026-09-30T23:59:59.999Z").version, "2026.1");
  assert.equal(bundle.policyFor("t.run", "2026-10-01T00:00:00Z").version, "2026.2");
  assert.equal(bundle.policyFor("t.run").version, "2026.2");
  assert.deepEqual(bundle.versionsFor("t.run").map((p) => p.version), ["2026.2", "2026.1"]);
  assert.equal(bundle.policies[0].effectiveTo, "2026-10-01T00:00:00Z");
  const before = engine.evaluate("t.run", { n: 5 }, "2026-06-01T10:00:00.000Z");
  assert.deepEqual([before.result, before.policyVersion, before.clause], ["allow", "2026.1", "a"]);
  const after = engine.evaluate("t.run", { n: 5 }, "2026-11-01T10:00:00.000Z");
  assert.deepEqual([after.result, after.policyVersion], ["deny", "2026.2"]);
});

test("a moment no version covers is unchecked and says which windows exist", { skip }, async () => {
  const engine = new CelPolicyEngine(await datedBundle({ ...v2, effective_to: "2027-01-01T00:00:00Z" }), { logger: quiet });
  const verdict = engine.evaluate("t.run", { n: 500 }, "2026-06-01T10:00:00.000Z");
  assert.equal(verdict.result, "unchecked");
  assert.equal(verdict.reason, "no version of the policy for t.run was in force at 2026-06-01T10:00:00.000Z: T-1@2026.2 (2026-10-01T00:00:00Z to 2027-01-01T00:00:00Z)");
  assert.equal(engine.evaluate("other.run", {}, "2026-06-01T10:00:00.000Z").reason, "no policy governs class other.run");
});

test("dated prefix policies are selected by date too, after exact matches", { skip }, async () => {
  const bundle = await datedBundle({ ...v1, classes: ["t.*"] }, { ...v2, classes: ["t.*"] }, { ...base, policy_id: "T-9", classes: ["t.exact"] });
  assert.equal(bundle.policyFor("t.other", "2026-06-01T00:00:00Z").version, "2026.1");
  assert.equal(bundle.policyFor("t.other", "2026-12-01T00:00:00Z").version, "2026.2");
  assert.equal(bundle.policyFor("t.exact", "2026-12-01T00:00:00Z").policyId, "T-9");
});

test("overlapping windows and malformed dates are load errors", { skip }, async () => {
  await assert.rejects(
    datedBundle(v1, { ...v2, effective_from: "2026-09-01" }),
    (err) => err instanceof PolicyError && /claimed by both T-1 \(the beginning to 2026-10-01T00:00:00Z\) and T-1 \(2026-09-01T00:00:00Z to further notice\)/.test(err.message),
  );
  await assert.rejects(datedBundle(v2, base), /claimed by both/);
  await assert.rejects(datedBundle({ ...base, effective_from: "next quarter" }), /effective_from is not a date or timestamp: "next quarter"/);
  await assert.rejects(datedBundle({ ...base, effective_from: "2026-10-01", effective_to: "2026-10-01" }), /effective_to must be after effective_from/);
});

test("check() passes the decision's opening time to the engine", { skip }, async () => {
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const seen = [];
  const inner = new CelPolicyEngine(await datedBundle({ ...v1, effective_to: "2020-01-01" }, { ...v2, effective_from: "2020-01-01" }), { logger: quiet });
  const policy = { evaluate(cls, inputs, at) { seen.push(at); return inner.evaluate(cls, inputs, at); } };
  const w = new Warrant("lending", { store: sink, policy, agent: { name: "a", version: "1" }, spillDir: await mkdtemp(join(tmpdir(), "warrant-policy-")), flushIntervalMs: 5, logger: quiet });
  await w.decide("t.run", { subject: "S-1" }, (d) => { d.check({ n: 500 }); });
  await w.flush();
  await w.close();
  // The opening time, not the record's: the record is stamped when the scope closes, which can be a millisecond later.
  assert.match(seen[0], /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  assert.ok(seen[0] <= sink.records[0].timestamp);
  assert.equal(sink.records[0].mandate.policy_version, "2026.2");
});
