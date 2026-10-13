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

const HELPERS = fileURLToPath(new URL("./temporal-workflows-helpers.js", import.meta.url));

/**
 * Run `helperWorkflow` once with the plugin on the worker. `review` is sent as the `review` Update;
 * `replayWith` replays the finished history against that workflows file and returns its error, if any.
 */
async function runHelpers(loan, mode, { review, replayWith, workflowType = "helperWorkflow", args, history = false } = {}) {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const { w, records } = await ledger();
  const plugin = () => adapter.warrantPlugin(w, { disburse: DISBURSE(adapter) });
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const worker = await Worker.create({
      connection: env.nativeConnection, taskQueue: "lending-helpers", workflowsPath: HELPERS,
      activities: activities(adapter.modelUsage, activity.Context), plugins: [plugin()],
    });
    const workflowId = `loan-${Date.now()}-${Math.random()}`;
    let out = await worker.runUntil(async () => {
      const handle = await env.client.workflow.start(workflowType, { args: args ?? [loan, mode], taskQueue: "lending-helpers", workflowId });
      if (review !== undefined) assert.equal(await handle.executeUpdate("review", { args: [review] }), "noted");
      return handle.result();
    });
    if (history) {
      const { events } = await env.client.workflow.getHandle(workflowId).fetchHistory();
      out = { result: out, failedTasks: events.filter((e) => e.workflowTaskFailedEventAttributes).length };
    }
    let replayError = null;
    if (replayWith) {
      const history = await env.client.workflow.getHandle(workflowId).fetchHistory();
      try {
        await Worker.runReplayHistory({ workflowsPath: replayWith, plugins: [plugin()] }, history, workflowId);
      } catch (err) {
        replayError = err;
      }
    }
    return { out, records: await records(), w, replayError };
  } finally {
    await env.teardown();
  }
}

test("a workflow-side decision is recorded through the local activity, and an upgrade behind patched() replays", { skip }, async () => {
  const { out, records, w, replayError } = await runHelpers(GOOD, "wf", { replayWith: fileURLToPath(new URL("./temporal-workflows-upgrade.js", import.meta.url)) });
  assert.equal(replayError, null);
  const { verdict } = out;
  assert.deepEqual([verdict.result, verdict.clause, verdict.status, verdict.flagged], ["allow", "4.2", "acted", false]);
  assert.equal(out.second, undefined);
  assert.equal(records.length, 1, "the replay wrote nothing");
  const [decision] = records;
  validate(decision);
  assert.equal(decision.record_id, verdict.record_id);
  assert.deepEqual(decision.decision, { class: "credit.approve", action: "approve", subject: "LN-1", status: "acted", summary: "workflow-side decision", alternatives: ["refer", "decline"] });
  assert.deepEqual(decision.evidence.map((e) => e.name), ["temporal.execution"]);
  assert.match(decision.evidence[0].uri, /^temporal:\/\/default\/loan-[^/]+\/[^/]+$/);
  await w.close();
});

test("the same upgrade without patched() fails replay, which is why patched() is needed for open executions", { skip }, async () => {
  const { replayError, w } = await runHelpers(GOOD, "wf", { replayWith: fileURLToPath(new URL("./temporal-workflows-unguarded.js", import.meta.url)) });
  assert.ok(replayError, "replay of the unguarded upgrade should fail");
  assert.match(`${replayError.name} ${replayError.message} ${replayError.cause?.message ?? ""}`, /[Dd]eterminism|[Nn]ondeterminism/);
  await w.close();
});

test("a retried workflow task records a workflow-side decision once", { skip }, async () => {
  const { out, records, w } = await runHelpers(GOOD, "", { workflowType: "retriedTaskWorkflow", args: [GOOD], history: true });
  assert.equal(out.failedTasks, 1, "the first workflow task failed and was retried");
  assert.equal(records.length, 2, "the record activity ran on both attempts");
  assert.deepEqual(records.map((r) => r.record_id), [out.result.record_id, out.result.record_id], "with the same record id, so the store keeps one");
  await w.close();
});

test("a workflow-side deny is withheld and the workflow branches on it", { skip }, async () => {
  const { out, records, w } = await runHelpers(WEAK, "wf");
  assert.deepEqual([out.verdict.result, out.verdict.status, out.verdict.clause], ["deny", "withheld", "4.1"]);
  const [decision] = records;
  assert.deepEqual([decision.decision.status, decision.decision.action], ["withheld", "none"]);
  await w.close();
});

test("an escalation approved by a person runs the activity and links the verdict", { skip }, async () => {
  const { out, records, w } = await runHelpers(BIG, "approve", { review: { approve: true, reviewer: "asha@bank.example", note: "DSCR checked" } });
  assert.equal(out.result, "disbursed LN-2");
  for (const r of records) validate(r);
  const decisions = records.filter((r) => r.record_type === "decision");
  const [verdict] = records.filter((r) => r.record_type === "human_verdict");
  const withheld = decisions.find((r) => r.decision.status === "withheld");
  const acted = decisions.find((r) => r.decision.status === "acted");
  assert.equal(decisions.length, 2);
  assert.equal(withheld.record_id, out.escalated);
  assert.equal(withheld.human.required, true);
  assert.deepEqual(verdict.references, { decision_record_id: withheld.record_id });
  assert.deepEqual([verdict.human.reviewer, verdict.human.verdict, verdict.human.note], ["asha@bank.example", "approve", "DSCR checked"]);
  assert.equal(acted.mandate.result, "escalate");
  assert.deepEqual(acted.human, { required: true, reviewer: "asha@bank.example", note: `approved after escalation ${withheld.record_id}` });
  assert.deepEqual(withheld.evidence.map((e) => e.name), ["temporal.execution", "underwrite"], "the escalation took the run's evidence");
  assert.deepEqual(acted.evidence.map((e) => e.name), ["temporal.execution", "warrant.escalation", "disburse.result", "anthropic/claude-sonnet-5"]);
  assert.equal(acted.evidence[1].uri, `warrant://record/${withheld.record_id}`);
  assert.equal(acted.evidence[1].content_hash, contentHash(withheld.record_id), "the same item the Python adapter writes");
  assert.ok(records.indexOf(verdict) < records.indexOf(acted), "the verdict is written before the activity runs");
  await w.close();
});

test("an escalation rejected by a person, or timed out, records the verdict and runs nothing", { skip }, async () => {
  const rejected = await runHelpers(BIG, "approve", { review: { approve: false, reviewer: "asha@bank.example", note: "income unverified" } });
  const timedOut = await runHelpers({ ...BIG, loan_id: "LN-4" }, "approve");
  assert.equal(rejected.out.verdict.verdict, "reject");
  assert.equal(timedOut.out.timed_out, true);
  for (const { records, out } of [rejected, timedOut]) {
    assert.deepEqual(records.map((r) => r.record_type).sort(), ["decision", "human_verdict"], "no acted record: nothing ran");
    const verdict = records.find((r) => r.record_type === "human_verdict");
    assert.equal(verdict.references.decision_record_id, out.escalated);
    assert.equal(verdict.record_id, out.verdict.record_id);
  }
  assert.deepEqual(
    [rejected, timedOut].map(({ records }) => records.find((r) => r.record_type === "human_verdict").human).map((h) => [h.reviewer, h.verdict, h.note]),
    [["asha@bank.example", "reject", "income unverified"], ["system", "reject", "timed out"]],
  );
  await rejected.w.close();
  await timedOut.w.close();
});

test("approved() refuses to run when the workflow's worker has no Warrant interceptor module", { skip }, async () => {
  const { TestWorkflowEnvironment, Worker, activity, adapter } = await load();
  const { w, records } = await ledger();
  const env = await TestWorkflowEnvironment.createTimeSkipping();
  try {
    const worker = await Worker.create({
      connection: env.nativeConnection, taskQueue: "lending-unattached", workflowsPath: HELPERS,
      activities: activities(adapter.modelUsage, activity.Context), interceptors: { activity: [adapter.warrantActivityInterceptor(w, { disburse: DISBURSE(adapter) })] },
    });
    const out = await worker.runUntil(env.client.workflow.execute("unattachedApprovalWorkflow", { args: [BIG, "01K58FEB00G9AC6AD9518FDN9S"], taskQueue: "lending-unattached", workflowId: `loan-${Date.now()}` }));
    assert.equal(out.attached, false);
    assert.match(out.message, /approval was not attached to the activity/);
    const written = await records();
    assert.ok(written.every((r) => r.record_type !== "human_verdict"), "no verdict for an approval that went nowhere");
    assert.ok(written.every((r) => r.decision?.status !== "acted"), "the activity did not run unapproved");
  } finally {
    await env.teardown();
    await w.close();
  }
});

test("a denial is not an escalation", async () => {
  const { ApplicationFailure } = await import("@temporalio/workflow");
  const { blocked, escalation } = await import("../src/adapters/temporal-workflow.js");
  const denied = ApplicationFailure.create({ message: "no", type: "WarrantDenied", nonRetryable: true, details: [{ record_id: "X", result: "deny" }] });
  assert.deepEqual(blocked(denied), { record_id: "X", result: "deny" });
  assert.equal(escalation(denied), null);
  assert.equal(blocked(new Error("other")), null);
});

test("the workflow helpers refuse bad arguments before scheduling anything", async () => {
  const wf = await import("../src/adapters/temporal-workflow.js");
  await assert.rejects(wf.decide("credit.approve", { subject: "", inputs: {}, action: "approve" }), /subject must be a non-empty string/);
  await assert.rejects(wf.decide("credit.approve", { subject: "LN-1", inputs: [], action: "approve" }), /inputs must be an object/);
  await assert.rejects(wf.approved("disburse", [], { reviewer: "", recordId: "01K58FEB00G9AC6AD9518FDN9S" }), /reviewer must be a non-empty string/);
  await assert.rejects(wf.approved("disburse", [], { reviewer: "a", recordId: "not-a-ulid" }), /recordId must be the escalated decision's record id/);
  await assert.rejects(wf.approved("disburse", { loan_id: "LN-1" }, { reviewer: "a", recordId: "01K58FEB00G9AC6AD9518FDN9S" }), /args must be the activity's arguments/);
  await assert.rejects(wf.verdict("01K58FEB00G9AC6AD9518FDN9S", { reviewer: "a", verdict: "maybe" }), /verdict must be approve, reject or amend/);
});

test("the record activity writes decisions and verdicts, and refuses a malformed request as unreadable", async () => {
  const { adapter } = await load();
  const { w, records } = await ledger();
  const record = adapter.warrantRecordActivity(w);
  const identity = { namespace: "default", workflow_type: "t", workflow_id: "wf-1", workflow_run_id: "run-1", first_execution_run_id: "run-1", task_queue: "q", attempt: 1 };
  const decided = await record({ kind: "decision", record_id: deterministicUlid(1758000000000, "a"), decision_class: "credit.approve", subject: "LN-2",
    inputs: { amount: 900000, bureau_score: 790, foir: 0.3 }, action: "approve", summary: null, alternatives: null, on_behalf_of: null, identity });
  assert.deepEqual([decided.status, decided.result, decided.policy_id], ["withheld", "escalate", "CR-07"]);
  const verdict = await record({ kind: "verdict", record_id: deterministicUlid(1758000000000, "b"), decision_record_id: decided.record_id, reviewer: "asha@bank.example", verdict: "approve", note: null });
  assert.equal(verdict.verdict, "approve");
  const [decision, linked] = await records();
  assert.deepEqual(decision.human, { required: true, note: "awaiting a human's decision" });
  assert.equal(decision.evidence[0].uri, "temporal://default/wf-1/run-1");
  assert.equal(linked.references.decision_record_id, decided.record_id);
  await assert.rejects(record({ kind: "decision", record_id: "x", decision_class: "c", subject: "s", inputs: [], action: "a" }), (err) => err.type === "WarrantUnreadable");
  await assert.rejects(record({ kind: "verdict", record_id: "x", decision_record_id: "y", reviewer: "a", verdict: "maybe" }), (err) => err.type === "WarrantUnreadable");
  await assert.rejects(record({ kind: "other" }), (err) => err.type === "WarrantUnreadable" && /unknown request kind/.test(err.message));
  await w.close();
});
