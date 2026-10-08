import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Warrant, deterministicUlid, validate } from "../src/index.js";

const [major, minor] = process.versions.node.split(".").map(Number);
const skip = major > 20 || (major === 20 && minor >= 3) ? false : "the Temporal TypeScript SDK needs Node 20.3+";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const quiet = { info() {}, warn() {}, error() {} };
const GOOD = { loan_id: "LN-1", amount: 450000, bureau_score: 748, foir: 0.38 };
const BIG = { loan_id: "LN-2", amount: 900000, bureau_score: 790, foir: 0.3 };
const WEAK = { loan_id: "LN-3", amount: 100000, bureau_score: 610, foir: 0.2 };

let temporal;
async function load() {
  if (temporal) return temporal;
  const [{ TestWorkflowEnvironment }, { DefaultLogger, Runtime, Worker }, activity, adapter, policy] = await Promise.all([
    import("@temporalio/testing"), import("@temporalio/worker"), import("@temporalio/activity"), import("../src/adapters/temporal.js"), import("../src/policy.js"),
  ]);
  Runtime.install({ logger: new DefaultLogger("WARN") });
  const bundle = await policy.PolicyBundle.load(join(ROOT, "examples", "policies"), { logger: quiet });
  temporal = { TestWorkflowEnvironment, Worker, activity, adapter, engine: new policy.CelPolicyEngine(bundle, { logger: quiet }) };
  return temporal;
}

async function ledger() {
  const { engine } = await load();
  const sink = { records: [], write(batch) { this.records.push(...batch); } };
  const w = new Warrant("lending", {
    tenant: "demo-bank", store: sink, agent: { name: "credit-underwriter", version: "2.4.0" }, currency: "INR",
    policy: engine, spillDir: await mkdtemp(join(tmpdir(), "warrant-temporal-")), flushIntervalMs: 5, logger: quiet,
  });
  const records = async () => {
    assert.equal(await w.flush(), true);
    return sink.records;
  };
  return { w, records };
}

function activities(modelUsage, Context) {
  return {
    async underwrite(loan) {
      return { loan_id: loan.loan_id, band: loan.bureau_score >= 700 ? "A" : "C" };
    },
    async disburse(loan) {
      modelUsage("anthropic", "claude-sonnet-5", { tokensIn: 1200, tokensOut: 80 });
      return `disbursed ${loan.loan_id}`;
    },
    async disburseFlaky(loan) {
      if (Context.current().info.attempt === 1) throw new Error("core banking timeout for PAN ABCDE1234F");
      return `disbursed ${loan.loan_id}`;
    },
    async disburseOpaque() {
      return "should not run";
    },
  };
}

async function run(guard, loan, mode, { plugins } = {}) {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const worker = await Worker.create({
      connection: env.nativeConnection, taskQueue: "lending-agents",
      workflowsPath: fileURLToPath(new URL("./temporal-workflows.js", import.meta.url)),
      activities: activities(adapter.modelUsage, activity.Context),
      ...(plugins ? { plugins } : { interceptors: { activity: [guard] } }),
    });
    return await worker.runUntil(env.client.workflow.execute("loanWorkflow", { args: [loan, mode], taskQueue: "lending-agents", workflowId: `loan-${Date.now()}-${Math.random()}` }));
  } finally {
    await env.teardown();
  }
}

const DISBURSE = (adapter) => new adapter.ActivityDecision({ decisionClass: "credit.approve", subject: "loan_id", action: "disburse", inputs: ["amount", "bureau_score", "foir"], costCentre: "retail-lending" });

test("an allowed activity runs and is recorded with evidence, identity and cost", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const out = await run(adapter.warrantActivityInterceptor(w, { disburse: DISBURSE(adapter) }, { pricer: (p, m, i, o) => i * 0.001 + o * 0.005 }), GOOD, "object");
  assert.deepEqual(out, { result: "disbursed LN-1" });
  const [decision] = await records();
  validate(decision);
  assert.deepEqual(decision.decision, { class: "credit.approve", action: "disburse", subject: "LN-1", status: "acted" });
  assert.equal(decision.mandate.result, "allow");
  assert.equal(decision.mandate.clause, "4.2");
  assert.deepEqual(decision.evidence.map((e) => e.name), ["temporal.execution", "underwrite", "disburse.result", "anthropic/claude-sonnet-5"]);
  const [identity, underwrite] = decision.evidence;
  assert.equal(identity.type, "other");
  assert.match(identity.uri, /^temporal:\/\/default\/loan-.*\/activity\/\d+\?attempt=1&type=disburse&queue=lending-agents$/);
  assert.deepEqual([underwrite.type, underwrite.uri], ["tool_call", "tool://underwrite#1"]);
  assert.deepEqual(decision.cost, { amount: 1.6, currency: "INR", cost_centre: "retail-lending", breakdown: [{ kind: "model_call", provider: "anthropic", model: "claude-sonnet-5", tokens_in: 1200, tokens_out: 80, amount: 1.6 }] });
  await w.close();
});

test("deny and escalate withhold and fail the activity without retry", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const guard = adapter.warrantActivityInterceptor(w, { disburse: DISBURSE(adapter) });
  const weak = await run(guard, WEAK, "object");
  const big = await run(guard, BIG, "object");
  assert.equal(weak.blocked, "WarrantDenied");
  assert.match(weak.message, /policy CR-07 clause 4\.1 does not allow it.*Do not retry/);
  assert.equal(big.blocked, "WarrantEscalated");
  assert.match(big.message, /requires a human to decide/);
  const bySubject = Object.fromEntries((await records()).map((r) => [r.decision.subject, r]));
  assert.deepEqual(Object.keys(bySubject).sort(), ["LN-2", "LN-3"], "one withheld record each: the retry policy did not re-run a blocked activity");
  assert.equal(weak.details.record_id, bySubject["LN-3"].record_id);
  assert.equal(weak.details.clause, "4.1");
  assert.deepEqual([bySubject["LN-3"].decision.status, bySubject["LN-3"].mandate.result], ["withheld", "deny"]);
  assert.equal(bySubject["LN-2"].mandate.result, "escalate");
  assert.deepEqual(bySubject["LN-2"].human, { required: true, note: "awaiting a human's decision; the activity was not run" });
  assert.deepEqual(bySubject["LN-2"].evidence.map((e) => e.name), ["temporal.execution", "underwrite"]);
  await w.close();
});

test("an unreadable call is blocked and is not a decision", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const out = await run(adapter.warrantActivityInterceptor(w, { disburseOpaque: DISBURSE(adapter) }), GOOD, "opaque");
  assert.equal(out.blocked, "WarrantUnreadable");
  assert.match(out.message, /no subject; expected a non-empty "loan_id"/);
  assert.deepEqual(await records(), []);
  await w.close();
});

test("a failed attempt is recorded without error text and the retry is a new record", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const out = await run(adapter.warrantActivityInterceptor(w, { disburseFlaky: DISBURSE(adapter) }), GOOD, "flaky");
  assert.deepEqual(out, { result: "disbursed LN-1" });
  const [failed, acted] = await records();
  assert.deepEqual([failed.decision.status, acted.decision.status], ["failed", "acted"]);
  assert.equal(JSON.stringify(failed).includes("ABCDE1234F"), false);
  assert.match(failed.evidence[0].uri, /\?attempt=1&/);
  assert.match(acted.evidence[0].uri, /\?attempt=2&/);
  assert.notEqual(failed.record_id, acted.record_id);
  assert.deepEqual(failed.evidence.map((e) => e.name), ["temporal.execution", "underwrite"], "a failed attempt does not consume the run's evidence");
  assert.deepEqual(acted.evidence.map((e) => e.name), ["temporal.execution", "underwrite", "disburseFlaky.result"]);
  await w.close();
});

test("record ids are deterministic and match the Python SDK", () => {
  assert.equal(deterministicUlid(1758000000000, "k"), "01K58FEB00G9AC6AD9518FDN9S");
  assert.equal(deterministicUlid(1758000000000, "ns|wf|run|3|1"), deterministicUlid(1758000000000, "ns|wf|run|3|1"));
  assert.notEqual(deterministicUlid(1758000000000, "ns|wf|run|3|1"), deterministicUlid(1758000000000, "ns|wf|run|3|2"));
});

test("the mapping reads the single object argument or a function of any call shape", { skip }, async () => {
  const { adapter } = await load();
  const byName = DISBURSE(adapter);
  assert.deepEqual(byName.read("disburse", GOOD), { subject: "LN-1", inputs: { amount: 450000, bureau_score: 748, foir: 0.38 } });
  const positional = new adapter.ActivityDecision({ decisionClass: "credit.approve", subject: ({ args }) => args[0], inputs: ({ args }) => ({ amount: args[1] }) });
  assert.deepEqual(positional.read("disburse", { args: ["LN-9", 5] }), { subject: "LN-9", inputs: { amount: 5 } });
  assert.throws(() => byName.read("disburse", { amount: 1 }), /no subject/);
  assert.throws(() => new adapter.ActivityDecision({ decisionClass: "credit.approve" }), /subject must be/);
  assert.throws(() => adapter.warrantActivityInterceptor({}, {}), /must be a Warrant/);
  assert.throws(() => adapter.modelUsage("anthropic", "claude-sonnet-5"), /activity/i);
});

test("the plugin installs the interceptor on a worker given no interceptors", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const plugin = adapter.warrantPlugin(w, { disburse: DISBURSE(adapter) });
  assert.equal(plugin.name, "warrantai.WarrantPlugin");
  const out = await run(null, GOOD, "object", { plugins: [plugin] });
  assert.deepEqual(out, { result: "disbursed LN-1" });
  const [decision] = await records();
  validate(decision);
  assert.deepEqual(decision.decision, { class: "credit.approve", action: "disburse", subject: "LN-1", status: "acted" });
  assert.equal(decision.mandate.clause, "4.2");
  await w.close();
});

test("the plugin keeps interceptors the worker already has and never adds its own twice", async () => {
  const { adapter } = await load();
  const { w } = await ledger();
  const plugin = adapter.warrantPlugin(w, { disburse: DISBURSE(adapter) });
  const other = () => ({});
  const once = plugin.configureWorker({ taskQueue: "q", interceptors: { activity: [other], workflowModules: ["x"] } });
  assert.equal(once.taskQueue, "q");
  assert.deepEqual(once.interceptors.workflowModules, ["x"]);
  assert.equal(once.interceptors.activity.length, 2);
  assert.equal(once.interceptors.activity[0], other);
  const twice = plugin.configureWorker(once);
  assert.equal(twice.interceptors.activity.length, 2);
  await w.close();
});
