// The two-activity loan workflow the Temporal adapter tests run. Bundled by the worker; imports only @temporalio/workflow.
import { ActivityFailure, ApplicationFailure, proxyActivities, proxyLocalActivities } from "@temporalio/workflow";

const { underwrite, disburse, disburseDown, disburseFlaky, disburseOpaque } = proxyActivities({
  startToCloseTimeout: "10s",
  retry: { initialInterval: "10ms", maximumAttempts: 3 },
});

export async function loanWorkflow(loan, mode) {
  await underwrite(loan);
  if (mode === "down") {
    try {
      await disburseDown(loan);
    } catch (err) {
      if (!(err instanceof ActivityFailure)) throw err;
    }
    return { result: await disburse(loan) };
  }
  try {
    let result;
    if (mode === "object") result = await disburse(loan);
    else if (mode === "flaky") result = await disburseFlaky(loan);
    else if (mode === "opaque") result = await disburseOpaque(loan, "note");
    else throw new Error(`unknown mode ${mode}`);
    return { result };
  } catch (err) {
    if (err instanceof ActivityFailure && err.cause instanceof ApplicationFailure) {
      return { blocked: err.cause.type, message: err.cause.message, details: err.cause.details?.[0] ?? null };
    }
    throw err;
  }
}

// The evidence and the decision on different task queues, so on different workers.
const bureau = proxyActivities({ taskQueue: "lending-bureau", startToCloseTimeout: "10s", retry: { initialInterval: "10ms", maximumAttempts: 3 } });
const payments = proxyActivities({ taskQueue: "lending-payments", startToCloseTimeout: "10s", retry: { initialInterval: "10ms", maximumAttempts: 3 } });

export async function spreadLoanWorkflow(loan) {
  await bureau.underwrite(loan);
  return await payments.disburse(loan);
}

// A local activity that backs off twice (each retry interval exceeds the local retry threshold, so
// each retry takes a new sequence number), then an activity, then a named one, then the decision.
const backoff = proxyLocalActivities({ startToCloseTimeout: "10s", localRetryThreshold: "1s", retry: { initialInterval: "2s", backoffCoefficient: 1, maximumAttempts: 5 } });
const plain = proxyActivities({ startToCloseTimeout: "10s", retry: { initialInterval: "10ms", maximumAttempts: 3 } });
const named = proxyActivities({ activityId: "bureau-check", startToCloseTimeout: "10s", retry: { initialInterval: "10ms", maximumAttempts: 3 } });

export async function backoffLoanWorkflow(loan) {
  const ids = [await backoff.lookupFlaky(loan), await plain.whoami(loan), await named.whoami(loan)];
  return { ids, result: await plain.disburse(loan) };
}
