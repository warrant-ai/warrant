/**
 * Workflow side of the Temporal adapter, bundled into workflow code: the outbound interceptor that
 * carries the run's evidence and a reviewer's approval to activities in headers, and helpers for
 * decisions and approvals made in workflow code.
 *
 *   import { approved, decide, escalation, rejected } from "warrantai/adapters/temporal/workflow";
 *
 *   const verdict = await decide("credit.approve", { subject: loan.id, inputs: { ... }, action: "approve" });
 *   try {
 *     return await payments.disburse(loan);
 *   } catch (err) {
 *     const escalated = escalation(err);
 *     if (!escalated) throw err;
 *     if (!(await condition(() => review !== undefined, "2 days"))) {
 *       return await rejected(escalated.record_id, { reviewer: "system", note: "timed out" });
 *     }
 *     if (review.approve) {
 *       return await approved("disburse", [loan], { reviewer: review.reviewer, recordId: escalated.record_id, startToCloseTimeout: "1 minute" });
 *     }
 *     return await rejected(escalated.record_id, { reviewer: review.reviewer, note: review.reason });
 *   }
 *
 * Evidence used to be kept in the activity worker's memory, so a decision that ran on a different
 * worker from the activities before it cited none of them. Workflow state replays on any worker.
 * `warrantPlugin()` registers this module for the worker's workflow bundle and the `warrant.record`
 * activity the helpers write through; with a prebuilt bundle, pass
 * `warrantWorkflowInterceptorModule(decisions)` to `bundleWorkflowCode({ workflowInterceptorModules })`.
 *
 * Everything here is deterministic: ids come from the workflow's `uuid4()` and the run's start time, and records
 * are written by the `warrant.record` local activity, whose result Temporal keeps in history, so
 * replay never writes twice. Because `decide()` schedules a local activity, adding a call to a
 * workflow type that already has open executions is a workflow change like any other: put it behind
 * `patched()` or use Worker Versioning. A workflow written with it needs neither.
 *
 * Imports only @temporalio/workflow and pure modules, since workflow code has no Node APIs.
 */

import { ActivityFailure, ApplicationFailure, defaultPayloadConverter, proxyActivities, proxyLocalActivities, uuid4, workflowInfo } from "@temporalio/workflow";

import { ULID_RE, deterministicUlid } from "../deterministic-ulid.js";
import { APPROVAL_HEADER, DENIED, ESCALATED, EVIDENCE_HEADER, MAX_EVIDENCE, RECORD_ACTIVITY, evidenceHash } from "./temporal-shared.js";

export { APPROVAL_HEADER, DENIED, ESCALATED, EVIDENCE_HEADER, RECORD_ACTIVITY };

const RECORD_OPTIONS = { startToCloseTimeout: "30s", retry: { maximumAttempts: 3 } };

// An approval waiting for the activity `approved()` is about to schedule, with that activity's type.
// Set and taken within one synchronous call, so no other workflow code runs in between, whatever
// this module's scope is shared with.
let pending = null;

/** The interceptors factory a workflow interceptor module exports, for the activity types that are decisions. */
export function warrantWorkflowInterceptors(decisionTypes) {
  const decisions = new Set(decisionTypes);
  return () => ({ outbound: [new EvidenceOutbound(decisions)] });
}

class EvidenceOutbound {
  constructor(decisions) {
    this._decisions = decisions;
    // Results of this run's activities since the last decision, as hashes. One interceptor per workflow run.
    this._evidence = [];
  }

  scheduleActivity(input, next) {
    let headers = input.headers;
    if (pending !== null && pending.activityType === input.activityType) {
      headers = { ...headers, [APPROVAL_HEADER]: defaultPayloadConverter.toPayload(pending.approval) };
      pending = null;
    }
    return this._track({ ...input, headers }, input.options?.activityId ?? `${input.seq}`, next);
  }

  scheduleLocalActivity(input, next) {
    return this._track(input, `${input.seq}`, next);
  }

  async _track(input, activityId, next) {
    if (this._decisions.has(input.activityType)) {
      const cited = [...this._evidence];
      const headers = cited.length ? { ...input.headers, [EVIDENCE_HEADER]: defaultPayloadConverter.toPayload(cited) } : input.headers;
      try {
        const result = await next({ ...input, headers });
        this._consume(cited);
        return result;
      } catch (err) {
        // A decision that was withheld has cited the evidence. One that failed for any other reason,
        // or was cancelled, leaves it for the next decision, as a failed attempt does for its retry.
        if (blocked(err)) this._consume(cited);
        throw err;
      }
    }
    const result = await next(input);
    if (input.activityType !== RECORD_ACTIVITY) {
      this._evidence.push({ name: input.activityType, uri: `tool://${input.activityType}#${activityId}`, content_hash: evidenceHash(result) });
      this._evidence = this._evidence.slice(-MAX_EVIDENCE);
    }
    return result;
  }

  _consume(cited) {
    this._evidence = this._evidence.filter((item) => !cited.includes(item));
  }
}

/**
 * The details of a decision the gate blocked (`WarrantDenied` or `WarrantEscalated`), else `null`.
 * The details carry `record_id`, `result`, `decision_class`, `subject`, `policy_id`, `clause`, `reason`.
 */
export function blocked(err) {
  const cause = err instanceof ActivityFailure ? err.cause : err;
  if (!(cause instanceof ApplicationFailure) || (cause.type !== DENIED && cause.type !== ESCALATED)) return null;
  const detail = cause.details?.[0];
  return detail !== null && typeof detail === "object" && !Array.isArray(detail) ? { ...detail } : null;
}

/** The details of an escalated decision, else `null`. A denial is not an escalation: nobody can approve it. */
export function escalation(err) {
  const detail = blocked(err);
  return detail !== null && detail.result === "escalate" ? detail : null;
}

/**
 * Record a decision the workflow itself makes, and return the mandate's verdict to branch on.
 * Allowed (or unchecked) decisions are recorded as acted with `action`; denied and escalated ones
 * as withheld. Resolves to `{ record_id, status, result, policy_id, policy_version, clause, reason, flagged }`.
 */
export async function decide(decisionClass, { subject, inputs, action, summary, alternatives, onBehalfOf } = {}) {
  requireText(decisionClass, "decisionClass");
  requireText(subject, "subject");
  requireText(action, "action");
  if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs)) throw new TypeError("inputs must be an object of policy input names to values");
  const request = {
    kind: "decision",
    record_id: recordIdFor("wf"),
    decision_class: decisionClass,
    subject,
    inputs: { ...inputs },
    action,
    summary: summary ?? null,
    alternatives: alternatives?.length ? [...alternatives] : null,
    on_behalf_of: onBehalfOf ?? null,
    identity: identity(),
  };
  return writeRecord(request, `warrant: ${decisionClass} for ${subject}`);
}

/**
 * Run an escalated activity now that a person has approved it: schedules `activityType` with
 * `args` and the activity `options` (timeouts, retry, task queue), carrying the approval. `reviewer`
 * is who approved, from your own Update or Signal payload; `recordId` is the escalated decision's,
 * from `escalation(err)`. The reviewer's verdict is appended to the ledger as its own sealed record
 * before the activity runs, and the activity's record names the reviewer.
 */
export async function approved(activityType, args, { reviewer, recordId, note, ...options } = {}) {
  requireText(activityType, "activityType");
  if (!Array.isArray(args)) throw new TypeError("args must be the activity's arguments, as an array");
  requireText(reviewer, "reviewer");
  if (typeof recordId !== "string" || !ULID_RE.test(recordId)) throw new TypeError("recordId must be the escalated decision's record id");
  const proxy = proxyActivities(options);
  pending = { activityType, approval: { record_id: recordId, reviewer, note: note ?? null } };
  let started;
  let attached;
  try {
    started = proxy[activityType](...args);
    attached = pending === null;
  } finally {
    pending = null;
  }
  if (!attached) {
    // Scheduled without the approval, so the gate escalates it again rather than running it; its outcome is not this call's.
    started.catch(() => {});
    throw new Error(
      "the approval was not attached to the activity: the worker running this workflow needs warrantPlugin(), " +
        "or warrantWorkflowInterceptorModule(decisions) in its workflow interceptor modules",
    );
  }
  return await started;
}

/**
 * Record a person's verdict on a decision that was routed to them, as a linked record: `approve`,
 * `reject` or `amend`. The reviewer's name comes from the application's own Update or Signal;
 * Warrant records who it was told, and never authenticates them.
 */
export async function verdict(recordId, { reviewer, verdict: given, note } = {}) {
  requireText(reviewer, "reviewer");
  if (!["approve", "reject", "amend"].includes(given)) throw new TypeError("verdict must be approve, reject or amend");
  if (typeof recordId !== "string" || !ULID_RE.test(recordId)) throw new TypeError("recordId must be the decision's record id");
  const request = { kind: "verdict", record_id: recordIdFor("verdict"), decision_record_id: recordId, reviewer, verdict: given, note: note ?? null };
  return writeRecord(request, `warrant: ${given} verdict on ${recordId}`);
}

/** Record that a person rejected an escalated decision, or that the wait for one timed out. */
export async function rejected(recordId, { reviewer, note } = {}) {
  return verdict(recordId, { reviewer, verdict: "reject", note });
}

function writeRecord(request, summary) {
  return proxyLocalActivities({ ...RECORD_OPTIONS, summary })[RECORD_ACTIVITY](request);
}

// Deterministic across a retried workflow task, which runs its local activities again at a later
// workflow time: the start time from the workflow's initialization and the run's seeded uuid4() are the
// same on the retry, so the store's duplicate check sees the repeat. Not `runStartTime`: the SDK takes
// that from the first activation, which a retried first task changes. The run id keeps runs apart.
function recordIdFor(tag) {
  const info = workflowInfo();
  return deterministicUlid(info.startTime.getTime(), `${info.namespace}|${info.workflowId}|${info.runId}|${tag}|${uuid4()}`);
}

function identity() {
  const info = workflowInfo();
  return {
    namespace: info.namespace, workflow_type: info.workflowType, workflow_id: info.workflowId,
    workflow_run_id: info.runId, first_execution_run_id: info.firstExecutionRunId,
    task_queue: info.taskQueue, attempt: info.attempt,
  };
}

function requireText(value, name) {
  if (typeof value !== "string" || !value) throw new TypeError(`${name} must be a non-empty string`);
}
