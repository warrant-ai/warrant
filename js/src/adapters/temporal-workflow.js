/**
 * Workflow side of the Temporal adapter: an outbound interceptor that keeps the run's evidence in
 * workflow state and hands it to the next decision activity in the `warrant-evidence` header.
 *
 * Evidence used to be kept in the activity worker's memory, so a decision that ran on a different
 * worker from the activities before it cited none of them. Workflow state replays on any worker.
 * `warrantPlugin()` registers this module for the worker's workflow bundle; with a prebuilt bundle,
 * pass `warrantWorkflowInterceptorModule(decisions)` to `bundleWorkflowCode({ workflowInterceptorModules })`.
 * Bundled into workflow code, so it imports only @temporalio/workflow and pure modules.
 */

import { ActivityFailure, ApplicationFailure, defaultPayloadConverter } from "@temporalio/workflow";

import { DENIED, ESCALATED, EVIDENCE_HEADER, MAX_EVIDENCE, RECORD_ACTIVITY, evidenceHash } from "./temporal-shared.js";

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
    return this._track(input, input.options?.activityId ?? `${input.seq}`, next);
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

function blocked(err) {
  return err instanceof ActivityFailure && err.cause instanceof ApplicationFailure && (err.cause.type === DENIED || err.cause.type === ESCALATED);
}
