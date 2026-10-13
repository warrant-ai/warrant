"""A workflow that imports the Warrant helpers with a plain import, for the plugin tests.

``WarrantPlugin`` passes ``warrant`` through the sandbox, so this module needs no
``workflow.unsafe.imports_passed_through()`` block. It must not import ``temporal_app``, whose
own passthrough block would hide what this module proves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow

from warrant.adapters import temporal_workflow as warrant


@dataclass
class Loan:
    loan_id: str
    amount: int
    bureau_score: int
    foir: float


@activity.defn
async def underwrite(loan: Loan) -> Dict[str, Any]:
    return {"loan_id": loan.loan_id, "band": "A" if loan.bureau_score >= 700 else "C"}


@workflow.defn
class PluginLoanWorkflow:
    @workflow.run
    async def run(self, loan: Loan) -> Dict[str, Any]:
        await workflow.execute_activity(underwrite, loan, start_to_close_timeout=timedelta(seconds=10))
        verdict = await warrant.decide(
            "credit.approve", subject=loan.loan_id, action="approve", summary="decided in the workflow",
            inputs={"amount": loan.amount, "bureau_score": loan.bureau_score, "foir": loan.foir},
        )
        return {"verdict": verdict}
