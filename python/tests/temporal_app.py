"""A two-activity loan workflow for the Temporal adapter tests.

Lives in its own module because Temporal's workflow sandbox re-imports the module that
defines a workflow, so it must import nothing that the sandbox restricts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=10), maximum_attempts=3)
TIMEOUT = timedelta(seconds=10)


@dataclass
class Loan:
    loan_id: str
    amount: int
    bureau_score: int
    foir: float


@activity.defn
async def underwrite(loan: Loan) -> Dict[str, Any]:
    """Evidence, not a decision."""
    return {"loan_id": loan.loan_id, "band": "A" if loan.bureau_score >= 700 else "C"}


@activity.defn
def disburse(loan_id: str, amount: int, bureau_score: int, foir: float) -> str:
    """The decision: a sync activity with positional arguments, reporting a model call."""
    from warrant.adapters.temporal import model_usage

    model_usage("anthropic", "claude-sonnet-5", tokens_in=1200, tokens_out=80)
    return f"disbursed {loan_id}"


@activity.defn
async def disburse_flaky(loan: Loan) -> str:
    """The decision as an async activity taking one dataclass; the first attempt fails with a PAN in the message."""
    if activity.info().attempt == 1:
        raise RuntimeError("core banking timeout for PAN ABCDE1234F")
    return f"disbursed {loan.loan_id}"


@activity.defn
async def disburse_opaque(loan: Loan, memo: str) -> str:
    """Mapped as a decision but the mapping expects a loan_id argument that is not there."""
    return "should not run"


@workflow.defn
class LoanWorkflow:
    @workflow.run
    async def run(self, loan: Loan, mode: str) -> Dict[str, Any]:
        await workflow.execute_activity(underwrite, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        try:
            if mode == "sync":
                result = await workflow.execute_activity(disburse, args=[loan.loan_id, loan.amount, loan.bureau_score, loan.foir], start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            elif mode == "flaky":
                result = await workflow.execute_activity(disburse_flaky, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            elif mode == "opaque":
                result = await workflow.execute_activity(disburse_opaque, args=[loan, "note"], start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            else:
                raise ValueError(mode)
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError):
                return {"blocked": cause.type, "message": cause.message, "details": cause.details[0] if cause.details else None}
            raise
        return {"result": result}
