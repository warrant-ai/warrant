// The two-activity loan workflow the Temporal adapter tests run. Bundled by the worker; imports only @temporalio/workflow.
import { ActivityFailure, ApplicationFailure, proxyActivities } from "@temporalio/workflow";

const { underwrite, disburse, disburseFlaky, disburseOpaque } = proxyActivities({
  startToCloseTimeout: "10s",
  retry: { initialInterval: "10ms", maximumAttempts: 3 },
});

export async function loanWorkflow(loan, mode) {
  await underwrite(loan);
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
