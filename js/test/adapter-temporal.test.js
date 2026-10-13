import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Warrant, contentHash, deterministicUlid, validate } from "../src/index.js";

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
    async disburseDown() {
      throw new Error("core banking unavailable");
    },
    async disburseFlaky(loan) {
      if (Context.current().info.attempt === 1) throw new Error("core banking timeout for PAN ABCDE1234F");
      return `disbursed ${loan.loan_id}`;
    },
    async disburseOpaque() {
      return "should not run";
    },
    async lookupFlaky() {
      if (Context.current().info.attempt < 3) throw new Error("lookup unavailable");
      return Context.current().info.activityId;
    },
    async whoami() {
      return Context.current().info.activityId;
    },
  };
}

/** Run the loan workflow once. `decisions` registers the workflow interceptor module beside `guard`, as the plugin would. */
async function run(guard, loan, mode, { plugins, decisions } = {}) {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const interceptors = { activity: [guard], workflowModules: decisions ? [adapter.warrantWorkflowInterceptorModule(decisions)] : [] };
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const worker = await Worker.create({
      connection: env.nativeConnection, taskQueue: "lending-agents",
      workflowsPath: fileURLToPath(new URL("./temporal-workflows.js", import.meta.url)),
      activities: activities(adapter.modelUsage, activity.Context),
      ...(plugins ? { plugins } : { interceptors }),
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
  const decisions = { disburse: DISBURSE(adapter) };
  const out = await run(adapter.warrantActivityInterceptor(w, decisions, { pricer: (p, m, i, o) => i * 0.001 + o * 0.005 }), GOOD, "object", { decisions });
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
  const decisions = { disburse: DISBURSE(adapter) };
  const guard = adapter.warrantActivityInterceptor(w, decisions);
  const weak = await run(guard, WEAK, "object", { decisions });
  const big = await run(guard, BIG, "object", { decisions });
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
  const decisions = { disburseFlaky: DISBURSE(adapter) };
  const out = await run(adapter.warrantActivityInterceptor(w, decisions), GOOD, "flaky", { decisions });
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

/**
 * Three workers, each with its own plugin, as three processes would have: the workflow on one, the
 * evidence activity on a second, the decision on a third. Replays the run afterwards.
 */
async function spread(workflowGuarded) {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const { w, records } = await ledger();
  const plugin = () => adapter.warrantPlugin(w, { disburse: DISBURSE(adapter) });
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const acts = activities(adapter.modelUsage, activity.Context);
    const workflowsPath = fileURLToPath(new URL("./temporal-workflows.js", import.meta.url));
    const flow = await Worker.create({ connection: env.nativeConnection, taskQueue: "lending-flow", workflowsPath, ...(workflowGuarded ? { plugins: [plugin()] } : {}) });
    const bureau = await Worker.create({ connection: env.nativeConnection, taskQueue: "lending-bureau", activities: { underwrite: acts.underwrite }, plugins: [plugin()] });
    const payments = await Worker.create({ connection: env.nativeConnection, taskQueue: "lending-payments", activities: { disburse: acts.disburse }, plugins: [plugin()] });
    const workflowId = `loan-${Date.now()}-${Math.random()}`;
    const out = await payments.runUntil(bureau.runUntil(flow.runUntil(env.client.workflow.execute("spreadLoanWorkflow", { args: [GOOD], taskQueue: "lending-flow", workflowId }))));
    const history = await env.client.workflow.getHandle(workflowId).fetchHistory();
    await Worker.runReplayHistory({ workflowsPath, plugins: [plugin()] }, history, workflowId);
    return { out, records: await records(), w };
  } finally {
    await env.teardown();
  }
}

test("the evidence reaches a decision that runs on another worker, and the run replays", { skip }, async () => {
  const { out, records, w } = await spread(true);
  assert.equal(out, "disbursed LN-1");
  const [decision] = records;
  validate(decision);
  assert.deepEqual(decision.evidence.map((e) => e.name), ["temporal.execution", "underwrite", "disburse.result", "anthropic/claude-sonnet-5"]);
  const underwrite = decision.evidence[1];
  assert.equal(underwrite.uri, "tool://underwrite#1");
  // The hash Python computes for the same result (python/tests/test_adapter_temporal.py), so either SDK can produce or read the header.
  assert.equal(underwrite.content_hash, contentHash({ loan_id: "LN-1", band: "A" }));
  await w.close();
});

test("evidence names the activity id Temporal assigned, even after a local activity backs off", { skip }, async () => {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const { w, records } = await ledger();
  const plugin = () => adapter.warrantPlugin(w, { disburse: DISBURSE(adapter) });
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const workflowsPath = fileURLToPath(new URL("./temporal-workflows.js", import.meta.url));
    const worker = await Worker.create({ connection: env.nativeConnection, taskQueue: "lending-backoff", workflowsPath,
      activities: activities(adapter.modelUsage, activity.Context), plugins: [plugin()] });
    const workflowId = `loan-${Date.now()}-${Math.random()}`;
    const out = await worker.runUntil(env.client.workflow.execute("backoffLoanWorkflow", { args: [GOOD], taskQueue: "lending-backoff", workflowId }));
    const history = await env.client.workflow.getHandle(workflowId).fetchHistory();
    await Worker.runReplayHistory({ workflowsPath, plugins: [plugin()] }, history, workflowId);
    const [lookedUp, checked, named] = out.ids;
    assert.notEqual(lookedUp, "1", "the local activity backed off, so its id is not the first sequence number");
    assert.equal(named, "bureau-check");
    const [decision] = await records();
    const cited = decision.evidence.map((e) => e.uri).filter((uri) => uri.startsWith("tool://") && uri.includes("#"));
    assert.deepEqual(cited, [`tool://lookupFlaky#${lookedUp}`, `tool://whoami#${checked}`, "tool://whoami#bureau-check"]);
  } finally {
    await env.teardown();
    await w.close();
  }
});

test("without the workflow interceptor on the workflow's worker, a decision cites no evidence", { skip }, async () => {
  const { out, records, w } = await spread(false);
  assert.equal(out, "disbursed LN-1");
  assert.deepEqual(records[0].evidence.map((e) => e.name), ["temporal.execution", "disburse.result", "anthropic/claude-sonnet-5"]);
  await w.close();
});

test("a decision that fails outright leaves the evidence for the next", { skip }, async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const decisions = { disburseDown: DISBURSE(adapter), disburse: DISBURSE(adapter) };
  const out = await run(adapter.warrantActivityInterceptor(w, decisions), GOOD, "down", { decisions });
  assert.deepEqual(out, { result: "disbursed LN-1" });
  const all = await records();
  const failed = all.filter((r) => r.decision.status === "failed");
  const [acted] = all.filter((r) => r.decision.status === "acted");
  assert.equal(failed.length, 3);
  for (const r of failed) assert.deepEqual(r.evidence.map((e) => e.name), ["temporal.execution", "underwrite"]);
  assert.deepEqual(acted.evidence.map((e) => e.name), ["temporal.execution", "underwrite", "disburse.result", "anthropic/claude-sonnet-5"]);
  await w.close();
});

test("the workflow-side evidence hash equals contentHash for any activity result", async () => {
  const { evidenceHash, asEvidenceContent } = await import("../src/adapters/temporal-shared.js");
  const values = ["disbursed LN-1", "", 0, -1.5e-7, 1e21, true, null, [1, "two", { three: 3 }], { b: 1, a: [{ d: undefined, c: "₹😀" }] }, { when: new Date(0) }, new Uint8Array([1, 2, 3])];
  for (let i = 0; i < 500; i++) values.push({ n: Math.random() * 10 ** ((i % 40) - 20), s: String.fromCharCode(...Array.from({ length: i % 9 }, () => Math.floor(Math.random() * 0xd7ff))), i });
  for (const value of values) assert.equal(evidenceHash(value), contentHash(asEvidenceContent(value)), JSON.stringify(value));
});

test("an undecodable evidence header blocks the decision as unreadable", { skip }, async () => {
  const { adapter } = await load();
  const { MockActivityEnvironment } = await import("@temporalio/testing");
  const { w, records } = await ledger();
  const { inbound } = adapter.warrantActivityInterceptor(w, { disburse: DISBURSE(adapter) })();
  const bad = { metadata: { encoding: new TextEncoder().encode("binary/plain") }, data: new Uint8Array([1]) };
  let ran = false;
  const env = new MockActivityEnvironment({ activityType: "disburse" });
  await assert.rejects(
    env.run(() => inbound.execute({ args: [GOOD], headers: { "warrant-evidence": bad } }, async () => { ran = true; })),
    (err) => err.type === "WarrantUnreadable" && /evidence header could not be decoded/.test(err.message),
  );
  assert.equal(ran, false);
  assert.deepEqual(await records(), []);
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
  assert.deepEqual(once.interceptors.workflowModules, ["x", adapter.warrantWorkflowInterceptorModule({ disburse: DISBURSE(adapter) })]);
  assert.equal(once.interceptors.activity.length, 2);
  assert.equal(once.interceptors.activity[0], other);
  const twice = plugin.configureWorker(once);
  assert.equal(twice.interceptors.activity.length, 2);
  assert.equal(twice.interceptors.workflowModules.length, 2);
  assert.deepEqual(plugin.configureReplayWorker({ interceptors: {} }).interceptors.workflowModules, [once.interceptors.workflowModules[1]]);
  await w.close();
});
