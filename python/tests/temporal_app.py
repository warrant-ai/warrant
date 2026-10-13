"""A two-activity loan workflow for the Temporal adapter tests.

Lives in its own module because Temporal's workflow sandbox re-imports the module that
defines a workflow, so it must import nothing that the sandbox restricts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from warrant.adapters import temporal_workflow as warrant

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


@activity.defn
async def disburse_down(loan: Loan) -> str:
    """The decision when core banking is down on every attempt."""
    raise RuntimeError("core banking unavailable")


@activity.defn
async def disburse_loan(loan: Loan) -> str:
    """The decision that may be escalated and then approved by a person."""
    return f"disbursed {loan.loan_id}"


@activity.defn
async def lookup_flaky(loan: Loan) -> str:
    """Fails twice, so with a short local retry threshold each retry is a backoff: a new sequence number."""
    if activity.info().attempt < 3:
        raise RuntimeError("lookup unavailable")
    return activity.info().activity_id


@activity.defn
async def whoami(loan: Loan) -> str:
    return activity.info().activity_id


@workflow.defn
class LoanWorkflow:
    def __init__(self) -> None:
        self._review: Optional[Dict[str, Any]] = None

    @workflow.update
    async def review(self, decision: Dict[str, Any]) -> str:
        self._review = decision
        return "noted"

    @workflow.run
    async def run(self, loan: Loan, mode: str) -> Dict[str, Any]:
        await workflow.execute_activity(underwrite, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        if mode == "wf":
            verdict = await warrant.decide("credit.approve", subject=loan.loan_id, inputs={"amount": loan.amount, "bureau_score": loan.bureau_score, "foir": loan.foir},
                                           action="approve", summary="workflow-side decision", alternatives=["refer", "decline"])
            return {"verdict": verdict}
        if mode == "approve":
            return await self._with_approval(loan)
        if mode == "down":
            try:
                await workflow.execute_activity(disburse_down, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            except ActivityError:
                pass
            return {"result": await workflow.execute_activity(disburse_loan, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)}
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

    async def _with_approval(self, loan: Loan) -> Dict[str, Any]:
        try:
            return {"result": await workflow.execute_activity(disburse_loan, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)}
        except ActivityError as exc:
            escalated = warrant.escalation(exc)
            if escalated is None:
                raise
        try:
            await workflow.wait_condition(lambda: self._review is not None, timeout=timedelta(days=2))
        except asyncio.TimeoutError:
            verdict = await warrant.rejected(escalated["record_id"], reviewer="system", note="timed out")
            return {"escalated": escalated["record_id"], "timed_out": True, "verdict": verdict}
        review = self._review or {}
        if review.get("approve"):
            result = await warrant.approved(disburse_loan, loan, reviewer=review["reviewer"], record_id=escalated["record_id"], note=review.get("note"),
                                            start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            return {"escalated": escalated["record_id"], "result": result}
        verdict = await warrant.rejected(escalated["record_id"], reviewer=review["reviewer"], note=review.get("note"))
        return {"escalated": escalated["record_id"], "verdict": verdict}


@workflow.defn(name="LoanWorkflow")
class LoanWorkflowPatched(LoanWorkflow):
    """The same workflow after a deploy that adds a second decision behind a patch, for the replay test."""

    @workflow.run
    async def run(self, loan: Loan, mode: str) -> Dict[str, Any]:
        out = await super().run(loan, mode)
        if workflow.patched("warrant-second-decision"):
            out["second"] = await warrant.decide("credit.approve", subject=loan.loan_id, inputs={"amount": loan.amount, "bureau_score": loan.bureau_score, "foir": loan.foir}, action="confirm")
        return out


@workflow.defn
class SpreadLoanWorkflow:
    """The evidence and the decision on different task queues, so on different workers."""

    @workflow.run
    async def run(self, loan: Loan) -> str:
        await workflow.execute_activity(underwrite, loan, task_queue="lending-bureau", start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        return await workflow.execute_activity(disburse_loan, loan, task_queue="lending-payments", start_to_close_timeout=TIMEOUT, retry_policy=RETRY)


@workflow.defn
class BackoffLoanWorkflow:
    """A local activity that backs off twice, then an activity, then the decision. Returns the ids Temporal assigned."""

    @workflow.run
    async def run(self, loan: Loan) -> Dict[str, Any]:
        backoff = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=1.0, maximum_attempts=5)
        looked_up = await workflow.execute_local_activity(lookup_flaky, loan, start_to_close_timeout=TIMEOUT,
                                                          local_retry_threshold=timedelta(seconds=1), retry_policy=backoff)
        checked = await workflow.execute_activity(whoami, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        named = await workflow.execute_activity(whoami, loan, activity_id="bureau-check", start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        result = await workflow.execute_activity(disburse_loan, loan, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
        return {"ids": [looked_up, checked, named], "result": result}


@workflow.defn
class RetriedTaskWorkflow:
    """Records a workflow-side decision, then fails its first workflow task, as a worker crash or an unhandled
    command would. The retried task runs the record local activity again, since its result reached no history."""

    @workflow.run
    async def run(self, loan: Loan) -> Dict[str, Any]:
        verdict = await warrant.decide("credit.approve", subject=loan.loan_id, inputs={"amount": loan.amount, "bureau_score": loan.bureau_score, "foir": loan.foir}, action="approve")
        if workflow.info().get_current_history_length() <= 3:
            raise RuntimeError("fail the first workflow task")
        return verdict
