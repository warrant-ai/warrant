// The same upgrade without patched(): replaying a history recorded before it must fail.
import { decide } from "warrantai/adapters/temporal/workflow";

import { helperWorkflow as before } from "./temporal-workflows-helpers.js";

export { review } from "./temporal-workflows-helpers.js";

export async function helperWorkflow(loan, mode) {
  const out = await before(loan, mode);
  const inputs = { amount: loan.amount, bureau_score: loan.bureau_score, foir: loan.foir };
  out.second = await decide("credit.approve", { subject: loan.loan_id, inputs, action: "confirm" });
  return out;
}
