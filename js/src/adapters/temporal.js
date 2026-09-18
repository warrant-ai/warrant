/**
 * Temporal adapter: an activity interceptor that gates and records the activities you name as decisions.
 *
 *   import { Worker } from "@temporalio/worker";
 *   import { ActivityDecision, warrantActivityInterceptor } from "warrantai/adapters/temporal";
 *
 *   const guard = warrantActivityInterceptor(w, {
 *     disburse: new ActivityDecision({ decisionClass: "credit.disburse", subject: "loan_id", inputs: ["amount", "bureau_score", "foir"] }),
 *   });
 *   const worker = await Worker.create({ ..., activities, interceptors: { activity: [guard] } });
 *
 * Workflow code does not change. Before a mapped activity runs, its arguments are checked
 * against the policy. `deny` and `escalate` stop it: the attempt is recorded as withheld and the
 * activity fails with a non-retryable ApplicationFailure of type `WarrantDenied` or
 * `WarrantEscalated`, which the workflow can catch and route to a person. `allow` and
 * `unchecked` change nothing. After the activity returns, the decision is recorded with the
 * run's other activity results attached as evidence, by hash, and with the Temporal identity
 * (namespace, workflow, run, activity, attempt) as evidence. An activity that throws is
 * recorded as a failed decision, without its error text.
 *
 * The arguments a mapping reads are the activity's single object argument, the usual Temporal
 * shape; any other shape is presented as `{ args }`, for mapping functions to read.
 *
 * One record per attempt, with a record id derived from the attempt's Temporal identity, so a
 * batch delivered twice is written once and a retry is a new record. Needs `@temporalio/activity`.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import { ApplicationFailure, Context } from "@temporalio/activity";

import { Verdict } from "../client.js";
import { contentHash } from "../hashing.js";
import { deterministicUlid } from "../ids.js";

export const DENIED = "WarrantDenied";
export const ESCALATED = "WarrantEscalated";
export const UNREADABLE = "WarrantUnreadable";

const MAX_EVIDENCE = 100;
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
 * prices reported model usage. `maxRuns` bounds how many runs' evidence is remembered.
 */
export function warrantActivityInterceptor(client, decisions, { evidence = () => true, pricer, maxRuns = 1000 } = {}) {
  if (!client || typeof client.decide !== "function") throw new TypeError("client must be a Warrant instance");
  if (!decisions || Object.keys(decisions).length === 0) throw new TypeError("decisions is empty: name at least one activity type whose runs are decisions");
  for (const [type, mapping] of Object.entries(decisions)) {
    if (!(mapping instanceof ActivityDecision)) throw new TypeError(`decisions[${JSON.stringify(type)}] must be an ActivityDecision`);
  }
  if (!Number.isInteger(maxRuns) || maxRuns < 1) throw new RangeError("maxRuns must be a positive integer");
  const log = new EvidenceLog(maxRuns);
  const execute = (input, next) => run(client, decisions, evidence, pricer, log, input, next);
  return () => ({ inbound: { execute } });
}

async function run(client, decisions, isEvidence, pricer, log, input, next) {
  const info = Context.current().info;
  const wf = info.workflowExecution ?? { workflowId: "", runId: "" };
  const runKey = `${wf.workflowId}/${wf.runId}`;
  const mapping = decisions[info.activityType];
  if (!mapping) {
    const result = await next(input);
    if (isEvidence(info.activityType)) log.add(runKey, info.activityType, info.activityId, result);
    return result;
  }

  let read;
  try {
    read = mapping.read(info.activityType, callObject(input.args));
  } catch (err) {
    if (!(err instanceof AdapterError)) throw err;
    client._log?.warn?.(`warrant: ${info.activityType} blocked, not a decision: ${err.message}`);
    throw ApplicationFailure.nonRetryable(`Warrant could not read the decision from this call: ${err.message}`, UNREADABLE);
  }
  const { subject, inputs } = read;
  const key = `${info.workflowNamespace ?? ""}|${wf.workflowId}|${wf.runId}|${info.activityId}|${info.attempt}`;
  const recordId = deterministicUlid(info.currentAttemptScheduledTimestampMs, key);
  const identity = identityEvidence(info, wf);
  const verdict = client.policy ? client.policy.evaluate(mapping.decisionClass, inputs) : new Verdict("unchecked", { reason: "no policy engine configured" });
  if (verdict.result === "deny" || verdict.result === "escalate") {
    const note = verdict.result === "escalate" ? "awaiting a human's decision; the activity was not run" : undefined;
    await record(client, mapping, info.activityType, subject, inputs, { status: "withheld", recordId, evidence: [identity, ...log.take(runKey)], humanNote: note, pricer });
    throw ApplicationFailure.nonRetryable(blockedMessage(info.activityType, verdict), verdict.result === "escalate" ? ESCALATED : DENIED, {
      record_id: recordId, decision_class: mapping.decisionClass, subject, result: verdict.result,
      policy_id: verdict.policyId ?? null, clause: verdict.clause ?? null, reason: verdict.reason ?? null,
    });
  }

  const used = [];
  let result;
  try {
    result = await usage.run(used, () => next(input));
  } catch (err) {
    // A failed attempt keeps the run's evidence for the next attempt. The error text is not recorded: it can carry customer data.
    await record(client, mapping, info.activityType, subject, inputs, { status: "failed", recordId, evidence: [identity, ...log.peek(runKey)], usage: used, pricer });
    throw err;
  }
  await record(client, mapping, info.activityType, subject, inputs, { status: "acted", recordId, evidence: [identity, ...log.take(runKey)], result, usage: used, pricer });
  return result;
}

async function record(client, mapping, activityType, subject, inputs, { status, recordId, evidence, result, usage: used = [], pricer, humanNote }) {
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
      if (verdict.result === "escalate") d.requireHuman({ note: humanNote });
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

function asEvidenceContent(value) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return String(value);
  }
}

/** Activity results seen in one run, kept as hashes only, waiting for the run's next decision. */
class EvidenceLog {
  constructor(maxRuns) {
    this._byRun = new Map();
    this._maxRuns = maxRuns;
  }

  add(run, activityType, activityId, result) {
    const items = this._byRun.get(run) ?? [];
    this._byRun.delete(run);
    items.push({ name: activityType, uri: `tool://${activityType}#${activityId}`, type: "tool_call", contentHash: contentHash(asEvidenceContent(result)) });
    this._byRun.set(run, items.slice(-MAX_EVIDENCE));
    while (this._byRun.size > this._maxRuns) this._byRun.delete(this._byRun.keys().next().value);
  }

  peek(run) {
    return [...(this._byRun.get(run) ?? [])];
  }

  take(run) {
    const items = this._byRun.get(run) ?? [];
    this._byRun.delete(run);
    return items;
  }
}
