/**
 * Temporal adapter: an activity interceptor that gates and records the activities you name as decisions.
 *
 *   import { Worker } from "@temporalio/worker";
 *   import { ActivityDecision, warrantActivityInterceptor } from "warrantai/adapters/temporal";
 *
 *   const plugin = warrantPlugin(w, {
 *     disburse: new ActivityDecision({ decisionClass: "credit.disburse", subject: "loan_id", inputs: ["amount", "bureau_score", "foir"] }),
 *   });
 *   const worker = await Worker.create({ ..., activities, plugins: [plugin] });
 *
 * The plugin adds the activity interceptor and registers the workflow interceptor module
 * (`temporal-workflow.js`) for the worker's workflow bundle. Without the plugin, give the worker
 * `interceptors: { activity: [warrantActivityInterceptor(...)], workflowModules: [warrantWorkflowInterceptorModule(decisions)] }`.
 * Put the plugin on every worker: the ones that run the workflow and the ones that run its activities.
 *
 * Gating and recording need no change to workflow code. Before a mapped activity runs, its arguments are checked
 * against the policy. `deny` and `escalate` stop it: the attempt is recorded as withheld and the
 * activity fails with a non-retryable ApplicationFailure of type `WarrantDenied` or
 * `WarrantEscalated`, which the workflow can catch and route to a person. `allow` and
 * `unchecked` change nothing. After the activity returns, the decision is recorded with the
 * run's earlier activity results attached as evidence, by hash, and with the Temporal identity
 * (namespace, workflow, run, activity, attempt) as evidence. The workflow interceptor keeps those
 * hashes in workflow state and passes them in the `warrant-evidence` header, so they reach the
 * decision on whichever worker runs it. An activity that throws is recorded as a failed decision,
 * without its error text. An escalated activity that workflow code runs again through `approved()`
 * (`warrantai/adapters/temporal/workflow`) carries the approval in the `warrant-approval` header:
 * the reviewer's verdict is written as its own record, linked to the escalated decision, before the
 * activity runs, and the activity's record names the reviewer. A denial cannot be approved.
 *
 * The arguments a mapping reads are the activity's single object argument, the usual Temporal
 * shape; any other shape is presented as `{ args }`, for mapping functions to read.
 *
 * One record per attempt, with a record id derived from the attempt's Temporal identity, so a
 * batch delivered twice is written once and a retry is a new record. Needs `@temporalio/activity`.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { ApplicationFailure, Context } from "@temporalio/activity";

import { Verdict } from "../client.js";
import { contentHash, sha256Hex } from "../hashing.js";
import { ULID_RE, deterministicUlid } from "../ids.js";
import { APPROVAL_HEADER, DENIED, ESCALATED, EVIDENCE_HEADER, MAX_EVIDENCE, RECORD_ACTIVITY, UNREADABLE, asEvidenceContent } from "./temporal-shared.js";

export { APPROVAL_HEADER, DENIED, ESCALATED, EVIDENCE_HEADER, RECORD_ACTIVITY, UNREADABLE };

const WORKFLOW_MODULE = fileURLToPath(new URL("./temporal-workflow.js", import.meta.url));
const usage = new AsyncLocalStorage();

class AdapterError extends Error {}
class ToolCallFailed extends Error {}

/** How one activity type's runs map to decisions. `subject` and `inputs` read the activity's arguments. */
export class ActivityDecision {
  constructor({ decisionClass, subject, action, inputs, costCentre } = {}) {
    if (typeof decisionClass !== "string" || !decisionClass) throw new TypeError("decisionClass is required");
    if (!(typeof subject === "string" && subject) && typeof subject !== "function") throw new TypeError("subject must be an argument name or a function of the arguments");
    if (inputs !== undefined && !Array.isArray(inputs) && typeof inputs !== "function") throw new TypeError("inputs must be a list of argument names, a function of the arguments, or undefined for every argument");
    this.decisionClass = decisionClass;
    this.subject = subject;
    this.action = action;
    this.inputs = inputs;
    this.costCentre = costCentre;
  }

  /** `{ subject, inputs }` for one call, or throws. */
  read(activityType, args) {
    let subject;
    let inputs;
    try {
      subject = typeof this.subject === "function" ? this.subject(args) : args[this.subject];
      if (typeof this.inputs === "function") inputs = { ...this.inputs(args) };
      else if (this.inputs === undefined) inputs = { ...args };
      else inputs = Object.fromEntries(this.inputs.filter((k) => k in args).map((k) => [k, args[k]]));
    } catch (err) {
      throw new AdapterError(`${activityType}: could not read the decision from the activity arguments: ${err?.constructor?.name ?? "Error"}`);
    }
    if (typeof subject === "number" && Number.isFinite(subject)) subject = String(subject);
    if (typeof subject !== "string" || !subject) {
      const name = typeof this.subject === "function" ? this.subject.name || "subject function" : this.subject;
      throw new AdapterError(`${activityType}: no subject; expected a non-empty ${JSON.stringify(name)} in the activity arguments`);
    }
    try {
      inputs = JSON.parse(JSON.stringify(inputs));
    } catch {
      throw new AdapterError(`${activityType}: policy inputs must be JSON values`);
    }
    if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs)) throw new AdapterError(`${activityType}: policy inputs must be an object`);
    return { subject, inputs };
  }
}

/** Report a model call made inside an activity, so its cost lands on that activity's decision. Ignored in unmapped activities. */
export function modelUsage(provider, model, { tokensIn = 0, tokensOut = 0 } = {}) {
  Context.current(); // throws outside an activity
  const items = usage.getStore();
  if (items) items.push({ provider, model, tokensIn: Math.trunc(tokensIn), tokensOut: Math.trunc(tokensOut) });
}

/**
 * Build the interceptor factory for `Worker.create({ interceptors: { activity: [factory] } })`.
 * `decisions` is keyed by activity type. `evidence(activityType)` says which unmapped activities are
 * evidence for the run's next decision (all, by default). `pricer(provider, model, tokensIn, tokensOut)`
 * prices reported model usage. The evidence itself arrives from the workflow interceptor; see
 * `warrantWorkflowInterceptorModule()`.
 */
export function warrantActivityInterceptor(client, decisions, { evidence = () => true, pricer } = {}) {
  if (!client || typeof client.decide !== "function") throw new TypeError("client must be a Warrant instance");
  if (!decisions || Object.keys(decisions).length === 0) throw new TypeError("decisions is empty: name at least one activity type whose runs are decisions");
  for (const [type, mapping] of Object.entries(decisions)) {
    if (!(mapping instanceof ActivityDecision)) throw new TypeError(`decisions[${JSON.stringify(type)}] must be an ActivityDecision`);
  }
  const execute = (input, next) => run(client, decisions, evidence, pricer, input, next);
  return () => ({ inbound: { execute } });
}

/**
 * The path of a workflow interceptor module for these decisions, for `interceptors.workflowModules`
 * or `bundleWorkflowCode({ workflowInterceptorModules })`. Workflow code runs in Temporal's sandbox
 * and cannot be handed the decision mapping at run time, so the module is written once per set of
 * activity types, under the system temporary directory, named by their hash.
 */
export function warrantWorkflowInterceptorModule(decisions) {
  const types = Object.keys(decisions ?? {}).sort();
  if (types.length === 0) throw new TypeError("decisions is empty: name at least one activity type whose runs are decisions");
  const source = `import { warrantWorkflowInterceptors } from ${JSON.stringify(WORKFLOW_MODULE)};\nexport const interceptors = warrantWorkflowInterceptors(${JSON.stringify(types)});\n`;
  const dir = join(tmpdir(), "warrantai-temporal");
  const path = join(dir, `workflow-${sha256Hex(source).slice(0, 16)}.js`);
  mkdirSync(dir, { recursive: true });
  // Written whole and renamed into place, so a worker starting at the same moment never bundles half a file.
  const partial = `${path}.${process.pid}.${Date.now()}.tmp`;
  writeFileSync(partial, source);
  renameSync(partial, path);
  return path;
}

/**
 * A worker plugin for `Worker.create({ plugins: [...] })` and replay workers: adds the activity
 * interceptor built by `warrantActivityInterceptor(client, decisions, options)`, the workflow
 * interceptor module from `warrantWorkflowInterceptorModule(decisions)`, and the `warrant.record`
 * activity from `warrantRecordActivity(client)` that the workflow-side helpers write through. Named
 * `warrantai.WarrantPlugin` in the worker's logs. Neither is added twice. A worker given a prebuilt
 * `workflowBundle` ignores `workflowModules`: pass the module to `bundleWorkflowCode` instead.
 */
export function warrantPlugin(client, decisions, options = {}) {
  const factory = warrantActivityInterceptor(client, decisions, options);
  const recordActivity = warrantRecordActivity(client);
  let workflowModule;
  const withInterceptors = (workerOptions) => {
    workflowModule ??= warrantWorkflowInterceptorModule(decisions);
    const activity = [...(workerOptions.interceptors?.activity ?? [])];
    if (!activity.includes(factory)) activity.push(factory);
    const workflowModules = [...(workerOptions.interceptors?.workflowModules ?? [])];
    if (!workflowModules.includes(workflowModule)) workflowModules.push(workflowModule);
    // The local activity the workflow-side helpers write through; one the worker already registers is kept.
    const activities = { [RECORD_ACTIVITY]: recordActivity, ...workerOptions.activities };
    return { ...workerOptions, activities, interceptors: { ...workerOptions.interceptors, activity, workflowModules } };
  };
  return {
    name: "warrantai.WarrantPlugin",
    configureWorker: withInterceptors,
    configureReplayWorker(replayOptions) {
      workflowModule ??= warrantWorkflowInterceptorModule(decisions);
      const workflowModules = [...(replayOptions.interceptors?.workflowModules ?? [])];
      if (!workflowModules.includes(workflowModule)) workflowModules.push(workflowModule);
      return { ...replayOptions, interceptors: { ...replayOptions.interceptors, workflowModules } };
    },
  };
}

async function run(client, decisions, isEvidence, pricer, input, next) {
  const info = Context.current().info;
  const wf = info.workflowExecution ?? { workflowId: "", runId: "" };
  const mapping = decisions[info.activityType];
  if (!mapping) return next(input);

  let read;
  let cited;
  let approval;
  try {
    read = mapping.read(info.activityType, callObject(input.args));
    cited = evidenceFrom(input.headers).filter((item) => isEvidence(item.name));
    approval = approvalFrom(input.headers);
  } catch (err) {
    if (!(err instanceof AdapterError)) throw err;
    client._log?.warn?.(`warrant: ${info.activityType} blocked, not a decision: ${err.message}`);
    throw ApplicationFailure.nonRetryable(`Warrant could not read the decision from this call: ${err.message}`, UNREADABLE);
  }
  const { subject, inputs } = read;
  const key = `${info.workflowNamespace ?? ""}|${wf.workflowId}|${wf.runId}|${info.activityId}|${info.attempt}`;
  const recordId = deterministicUlid(info.currentAttemptScheduledTimestampMs, key);
  const identity = [identityEvidence(info, wf)];
  const verdict = client.policy ? client.policy.evaluate(mapping.decisionClass, inputs) : new Verdict("unchecked", { reason: "no policy engine configured" });
  if (verdict.result === "deny" || (verdict.result === "escalate" && approval === null)) {
    const note = verdict.result === "escalate" ? "awaiting a human's decision; the activity was not run" : undefined;
    await record(client, mapping, info.activityType, subject, inputs, { status: "withheld", recordId, evidence: [...identity, ...cited], humanNote: note, pricer });
    throw ApplicationFailure.nonRetryable(blockedMessage(info.activityType, verdict), verdict.result === "escalate" ? ESCALATED : DENIED, {
      record_id: recordId, decision_class: mapping.decisionClass, subject, result: verdict.result,
      policy_id: verdict.policyId ?? null, clause: verdict.clause ?? null, reason: verdict.reason ?? null,
    });
  }

  let human = {};
  if (approval !== null) {
    // The reviewer's verdict is its own sealed record, linked to the escalated decision, written before the activity runs.
    client.humanVerdict({ reviewer: approval.reviewer, verdict: "approve", decisionRecordId: approval.record_id, note: approval.note ?? undefined,
      recordId: deterministicUlid(info.currentAttemptScheduledTimestampMs, `${key}|verdict`) });
    identity.push({ name: "warrant.escalation", uri: `warrant://record/${approval.record_id}`, type: "other", contentHash: contentHash(approval.record_id) });
    human = { humanReviewer: approval.reviewer, humanNote: `approved after escalation ${approval.record_id}` };
  }

  const used = [];
  let result;
  try {
    result = await usage.run(used, () => next(input));
  } catch (err) {
    // The error text is not recorded: it can carry customer data.
    await record(client, mapping, info.activityType, subject, inputs, { status: "failed", recordId, evidence: [...identity, ...cited], usage: used, pricer, ...human });
    throw err;
  }
  await record(client, mapping, info.activityType, subject, inputs, { status: "acted", recordId, evidence: [...identity, ...cited], result, usage: used, pricer, ...human });
  return result;
}

async function record(client, mapping, activityType, subject, inputs, { status, recordId, evidence, result, usage: used = [], pricer, humanNote, humanReviewer }) {
  let verdict;
  let id = recordId;
  try {
    await client.decide(mapping.decisionClass, { subject, recordId }, (d) => {
      id = d.recordId;
      verdict = d.check(inputs);
      for (const item of evidence.slice(-MAX_EVIDENCE)) d.evidence(item.name, { uri: item.uri, type: item.type, contentHash: item.contentHash });
      if (result !== undefined) d.evidence(`${activityType}.result`, { uri: `tool://${activityType}`, type: "tool_call", content: asEvidenceContent(result) });
      for (const call of used) {
        let amount = 0;
        if (pricer) {
          try {
            amount = Number(pricer(call.provider, call.model, call.tokensIn, call.tokensOut)) || 0;
          } catch (err) {
            client._log?.warn?.(`warrant: pricer failed for ${call.provider}/${call.model}, recording cost 0: ${err?.constructor?.name}`);
          }
        }
        d.modelCall(call.provider, call.model, { tokensIn: call.tokensIn, tokensOut: call.tokensOut, amount });
      }
      if (humanReviewer || verdict.result === "escalate") d.requireHuman({ reviewer: humanReviewer, note: humanNote });
      if (status === "acted") d.act(mapping.action ?? activityType, { costCentre: mapping.costCentre });
      else if (status === "failed") throw new ToolCallFailed("ToolCallFailed");
    });
  } catch (err) {
    if (!(err instanceof ToolCallFailed)) throw err;
  }
  client._log?.info?.(`warrant adapter recorded ${id} class=${mapping.decisionClass} status=${status} mandate=${verdict.result}`);
  return { recordId: id, verdict };
}

function blockedMessage(activityType, verdict) {
  const where = verdict.policyId ? `policy ${verdict.policyId}${verdict.clause ? ` clause ${verdict.clause}` : ""}` : "the policy";
  const why = verdict.reason ? ` (${verdict.reason})` : "";
  if (verdict.result === "escalate") {
    return `${activityType} was not carried out: ${where} requires a human to decide this${why}. Do not retry. Hand the case to a human reviewer and say why.`;
  }
  return `${activityType} was not carried out: ${where} does not allow it${why}. Do not retry with changed arguments. Tell the user what the policy says.`;
}

function callObject(args) {
  const [only] = args;
  if (args.length === 1 && only !== null && typeof only === "object" && !Array.isArray(only)) return only;
  return { args: [...args] };
}

function identityEvidence(info, wf) {
  const fields = {
    namespace: info.workflowNamespace ?? null, workflow_type: info.workflowType ?? null, workflow_id: wf.workflowId, workflow_run_id: wf.runId,
    activity_type: info.activityType, activity_id: info.activityId, attempt: info.attempt, task_queue: info.taskQueue,
  };
  const uri = `temporal://${info.workflowNamespace ?? ""}/${wf.workflowId}/${wf.runId}/activity/${info.activityId}?attempt=${info.attempt}&type=${info.activityType}&queue=${info.taskQueue}`;
  return { name: "temporal.execution", uri, type: "other", contentHash: contentHash(fields) };
}

/** The run's evidence the workflow interceptor attached, or none when the workflow's worker has no interceptor. */
function evidenceFrom(headers) {
  const payload = headers?.[EVIDENCE_HEADER];
  if (payload === undefined) return [];
  try {
    // Written by defaultPayloadConverter in workflow code (or the Python adapter's), always as json/plain.
    const encoding = new TextDecoder().decode(payload.metadata?.encoding);
    if (encoding !== "json/plain") throw new TypeError(`encoding ${encoding}`);
    const items = JSON.parse(new TextDecoder().decode(payload.data));
    return items.map((item) => {
      if (typeof item.name !== "string" || typeof item.uri !== "string" || typeof item.content_hash !== "string") throw new TypeError("item shape");
      return { name: item.name, uri: item.uri, type: "tool_call", contentHash: item.content_hash };
    });
  } catch (err) {
    throw new AdapterError(`the evidence header could not be decoded: ${err?.constructor?.name ?? "Error"}`);
  }
}

/** The approval `approved()` attached in workflow code (either SDK's), or `null`. */
function approvalFrom(headers) {
  const payload = headers?.[APPROVAL_HEADER];
  if (payload === undefined) return null;
  let approval;
  try {
    const encoding = new TextDecoder().decode(payload.metadata?.encoding);
    if (encoding !== "json/plain") throw new TypeError(`encoding ${encoding}`);
    approval = JSON.parse(new TextDecoder().decode(payload.data));
  } catch (err) {
    throw new AdapterError(`the approval header could not be decoded: ${err?.constructor?.name ?? "Error"}`);
  }
  const { reviewer, record_id: recordId, note } = approval ?? {};
  if (typeof reviewer !== "string" || !reviewer || typeof recordId !== "string" || !ULID_RE.test(recordId)) {
    throw new AdapterError("the approval header needs a reviewer and the escalated decision's record id");
  }
  return { reviewer, record_id: recordId, note: typeof note === "string" ? note : null };
}

/**
 * The `warrant.record` activity that the workflow-side helpers (`decide()`, `verdict()`,
 * `rejected()`) write through, as a local activity. `warrantPlugin()` registers it; without the
 * plugin, add it to the worker's activities under `RECORD_ACTIVITY`.
 */
export function warrantRecordActivity(client) {
  if (!client || typeof client.decide !== "function") throw new TypeError("client must be a Warrant instance");
  return async function warrantRecord(request) {
    if (request === null || typeof request !== "object" || Array.isArray(request)) {
      throw ApplicationFailure.nonRetryable("warrant.record needs a request object", UNREADABLE);
    }
    try {
      if (request.kind === "decision") return await workflowDecision(client, request);
      if (request.kind === "verdict") return workflowVerdict(client, request);
    } catch (err) {
      if (!(err instanceof TypeError) && !(err instanceof RangeError)) throw err;
      throw ApplicationFailure.nonRetryable(`warrant.record: ${err.message}`, UNREADABLE);
    }
    throw ApplicationFailure.nonRetryable(`warrant.record: unknown request kind ${JSON.stringify(request.kind)}`, UNREADABLE);
  };
}

async function workflowDecision(client, request) {
  const { inputs } = request;
  if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs)) throw new TypeError("inputs must be an object with string keys");
  const identity = request.identity ?? {};
  const uri = `temporal://${identity.namespace}/${identity.workflow_id}/${identity.workflow_run_id}`;
  let verdict;
  let status = "withheld";
  let id = request.record_id;
  await client.decide(request.decision_class, { subject: request.subject, onBehalfOf: request.on_behalf_of ?? undefined, alternatives: request.alternatives ?? undefined, recordId: request.record_id }, (d) => {
    id = d.recordId;
    verdict = d.check(inputs);
    d.evidence("temporal.execution", { uri, type: "other", content: { ...identity } });
    if (verdict.result === "escalate") d.requireHuman({ note: "awaiting a human's decision" });
    if (verdict.result === "allow" || verdict.result === "unchecked") {
      d.act(request.action, { summary: request.summary ?? undefined });
      status = "acted";
    }
  });
  client._log?.info?.(`warrant adapter recorded ${id} class=${request.decision_class} status=${status} mandate=${verdict.result} (workflow side)`);
  return {
    record_id: id, status, result: verdict.result, policy_id: verdict.policyId ?? null, policy_version: verdict.policyVersion ?? null,
    clause: verdict.clause ?? null, reason: verdict.reason ?? null, flagged: verdict.flagged,
  };
}

function workflowVerdict(client, request) {
  const id = client.humanVerdict({ reviewer: request.reviewer, verdict: request.verdict, decisionRecordId: request.decision_record_id, note: request.note ?? undefined, recordId: request.record_id });
  return { record_id: id, verdict: request.verdict };
}
