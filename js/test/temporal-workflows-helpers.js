// Workflows that use the workflow-side helpers, imported the way an application imports them.
import { condition, defineUpdate, proxyActivities, setHandler, workflowInfo } from "@temporalio/workflow";

import { approved, decide, escalation, rejected } from "warrantai/adapters/temporal/workflow";

const ACTIVITY = { startToCloseTimeout: "10s", retry: { initialInterval: "10ms", maximumAttempts: 3 } };
const { underwrite, disburse } = proxyActivities(ACTIVITY);
export const review = defineUpdate("review");

/** `wf` records a workflow-side decision; `approve` runs the escalation path with a reviewer's Update or a two-day timeout. */
export async function helperWorkflow(loan, mode) {
  let decision;
  setHandler(review, (d) => {
    decision = d;
    return "noted";
  });
  await underwrite(loan);
  if (mode === "wf") {
    const inputs = { amount: loan.amount, bureau_score: loan.bureau_score, foir: loan.foir };
    return { verdict: await decide("credit.approve", { subject: loan.loan_id, inputs, action: "approve", summary: "workflow-side decision", alternatives: ["refer", "decline"] }) };
  }
  try {
    return { result: await disburse(loan) };
  } catch (err) {
    const escalated = escalation(err);
    if (escalated === null) throw err;
    if (!(await condition(() => decision !== undefined, "2 days"))) {
      return { escalated: escalated.record_id, timed_out: true, verdict: await rejected(escalated.record_id, { reviewer: "system", note: "timed out" }) };
    }
    if (decision.approve) {
      const result = await approved("disburse", [loan], { reviewer: decision.reviewer, recordId: escalated.record_id, note: decision.note, ...ACTIVITY });
      return { escalated: escalated.record_id, result };
    }
    return { escalated: escalated.record_id, verdict: await rejected(escalated.record_id, { reviewer: decision.reviewer, note: decision.note }) };
  }
}

/** `approved()` on a worker whose workflow bundle lacks Warrant's interceptor module. */
export async function unattachedApprovalWorkflow(loan, recordId) {
  try {
    await approved("disburse", [loan], { reviewer: "asha@bank.example", recordId, ...ACTIVITY });
    return { attached: true };
  } catch (err) {
    return { attached: false, message: err.message };
  }
}

/**
 * Records a workflow-side decision, then fails its first workflow task, as a worker crash or an unhandled
 * command would. The retried task runs the record local activity again, since its result reached no history.
 */
export async function retriedTaskWorkflow(loan) {
  const inputs = { amount: loan.amount, bureau_score: loan.bureau_score, foir: loan.foir };
  const verdict = await decide("credit.approve", { subject: loan.loan_id, inputs, action: "approve" });
  if (workflowInfo().historyLength <= 3) throw new Error("fail the first workflow task");
  return verdict;
}
