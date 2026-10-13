// helperWorkflow after an upgrade that adds a second decide(), behind patched(): replays a history recorded before it.
import { patched } from "@temporalio/workflow";

import { decide } from "warrantai/adapters/temporal/workflow";

import { helperWorkflow as before } from "./temporal-workflows-helpers.js";

export { review } from "./temporal-workflows-helpers.js";

export async function helperWorkflow(loan, mode) {
  const out = await before(loan, mode);
  if (patched("warrant-second-decision")) {
    const inputs = { amount: loan.amount, bureau_score: loan.bureau_score, foir: loan.foir };
    out.second = await decide("credit.approve", { subject: loan.loan_id, inputs, action: "confirm" });
  }
  return out;
}
