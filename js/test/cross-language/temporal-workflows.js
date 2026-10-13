// A JS workflow that approves an escalated activity run by a Python worker (python/tests/test_temporal_cross_language.py).
import { approved } from "warrantai/adapters/temporal/workflow";

export async function crossLanguageApproval(loan, recordId, reviewer, note, activityQueue) {
  return approved("disburse_loan", [loan], { reviewer, recordId, note, taskQueue: activityQueue, startToCloseTimeout: "10s", retry: { maximumAttempts: 1 } });
}
