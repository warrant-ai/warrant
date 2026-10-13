"""The three layers working together: Temporal endures, a decision model decides, Warrant proves.

One alert, one workflow, for ninety days:

1. The workflow starts an activity that adjudicates the alert. That activity calls the decision
   model through :class:`~warrant.adapters.model.DecisionAdapter`, which evaluates the questions,
   checks the mandate, and writes the decision record in the same operation. Because the call
   happens inside an activity, the Temporal execution lands on the record by itself and the record
   id is derived from the attempt, so a retry is a new record and a re-sent batch is not.
2. If the policy routed the alert to a person, the workflow waits — for hours or weeks — on the
   application's own signal. The reviewer's verdict is written as a linked record.
3. Either way the workflow then sets a durable timer at the horizon where the truth becomes
   knowable. Ninety days later it wakes, asks the system of record what actually happened, and
   writes the outcome as a linked record.

Step 3 is the unglamorous part, and it is the reason Temporal earns its place in the stack rather
than a cron table. Nothing else survives a deploy, a restart and a quarter to close the loop, and
without that loop there is no calibration, which is the only thing here a competitor cannot copy.

**The composition rule.** ``adjudicate_alert`` is deliberately *not* listed in the Temporal
adapter's ``ToolDecision`` mapping. The model adapter owns the record because it holds the answers,
the confidences and the state digest; listing the activity as well would record the same decision
twice. Activities that act without a model — ``close_alert`` here — are what that mapping is for.

Lives in its own module because Temporal's workflow sandbox re-imports the module that defines a
workflow, so it must import nothing the sandbox restricts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from warrant.adapters import temporal_workflow as warrant

RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=50), maximum_attempts=3)
ACTIVITY_TIMEOUT = timedelta(seconds=30)
#: Where the truth about an alert becomes knowable. The single most important number in the file:
#: too short and the outcome is not yet real, too long and calibration arrives after the decision
#: it should have informed.
OUTCOME_HORIZON = timedelta(days=90)


@dataclass
class Alert:
    """Derived state only. Ratios, bands and flags — never account numbers, names or raw rows."""

    alert_id: str
    segment: str
    profile_consistent: bool
    structuring_pattern: bool
    counterparty_risk: float
    explanation_on_file: bool
    behaviour_change: float
    amount_band: str

    def state(self) -> Dict[str, Any]:
        return {
            "segment": self.segment,
            "profile_consistent": self.profile_consistent,
            "structuring_pattern": self.structuring_pattern,
            "counterparty_risk": self.counterparty_risk,
            "explanation_on_file": self.explanation_on_file,
            "behaviour_change": self.behaviour_change,
            "amount_band": self.amount_band,
        }


@dataclass
class Adjudication:
    """What the adjudicating activity hands back to the workflow."""

    record_id: str
    route: str
    disposition: str
    confidence: float
    model: str


@dataclass
class AlertResult:
    """The whole ninety days, as the workflow saw it."""

    alert_id: str
    record_id: str
    route: str
    disposition: str
    reviewer: Optional[str] = None
    outcome: Optional[str] = None
    outcome_record_id: Optional[str] = None
    history: list = field(default_factory=list)


@workflow.defn
class AlertAdjudication:
    """One alert, from raised to outcome observed."""

    def __init__(self) -> None:
        self._review: Optional[Dict[str, str]] = None
        self._observed: Optional[str] = None

    @workflow.signal
    def reviewed(self, reviewer: str, verdict: str, note: str = "") -> None:
        """The application's own review signal. Warrant records the reviewer, never authenticates them."""
        self._review = {"reviewer": reviewer, "verdict": verdict, "note": note}

    @workflow.signal
    def outcome_observed(self, label: str) -> None:
        """Lets a caller deliver the realised outcome early, rather than waiting for the horizon."""
        self._observed = label

    @workflow.run
    async def run(self, alert: Alert) -> AlertResult:
        adjudication: Adjudication = await workflow.execute_activity(
            adjudicate_alert,
            alert,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        result = AlertResult(
            alert_id=alert.alert_id,
            record_id=adjudication.record_id,
            route=adjudication.route,
            disposition=adjudication.disposition,
        )
        result.history.append(f"adjudicated by {adjudication.model}, route {adjudication.route}")

        if adjudication.route == "auto":
            await workflow.execute_activity(
                close_alert,
                args=[alert.alert_id, adjudication.disposition],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            )
            result.history.append("closed automatically")
        else:
            # Hours or weeks. Temporal holds the case open across deploys and restarts; a timeout
            # here is itself an audit finding, so it is recorded rather than swallowed.
            await workflow.wait_condition(lambda: self._review is not None)
            review = self._review or {}
            result.reviewer = review.get("reviewer")
            if review.get("verdict") == "approve":
                await warrant.verdict(
                    adjudication.record_id,
                    reviewer=review["reviewer"],
                    verdict="approve",
                    note=review.get("note") or None,
                )
                await workflow.execute_activity(
                    close_alert,
                    args=[alert.alert_id, adjudication.disposition],
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=RETRY,
                )
                result.history.append(f"closed after review by {review['reviewer']}")
            else:
                await warrant.rejected(
                    adjudication.record_id,
                    reviewer=review.get("reviewer", "unknown"),
                    note=review.get("note") or None,
                )
                result.history.append("kept open after review")

        # The durable half. Ninety days is not a sleep; the workflow is not resident in memory for
        # it, and it survives everything that happens to the fleet in between.
        try:
            await workflow.wait_condition(
                lambda: self._observed is not None, timeout=OUTCOME_HORIZON
            )
        except asyncio.TimeoutError:
            pass
        label = self._observed or await workflow.execute_activity(
            look_up_outcome,
            alert.alert_id,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        if not label:
            # The horizon passed and the system of record still cannot say. That is a real state,
            # not an error: the decision stays outcome-pending and is excluded from every
            # reliability curve until it is not. Recording a guess here would be the one thing
            # that makes a calibration claim worthless.
            result.history.append("no outcome observable at the horizon; left pending")
            return result
        result.outcome = label
        result.outcome_record_id = await workflow.execute_activity(
            record_outcome,
            args=[adjudication.record_id, label],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        result.history.append(f"outcome {label} observed and linked")
        return result


# --- activities -------------------------------------------------------------
#
# These are registered by the worker in run_platform.py. `adjudicate_alert` is the one that makes a
# decision, and it is deliberately absent from the ToolDecision mapping: the model adapter records
# it. `close_alert` is the acting activity that mapping is for.


@activity.defn
async def adjudicate_alert(alert: Alert) -> Adjudication:
    """Ask the model, check the mandate, write the record — one call, inside the activity."""
    adapter = current_adapter()
    result = adapter.decide(
        decision_class="aml.alert.disposition",
        subject=f"alert:{alert.alert_id}",
        state=alert.state(),
        questions=current_questions(),
        question_set=("aml.alert", "3.1.0"),
        cost_centre="fiu-ops",
    )
    disposition = result["disposition"]
    return Adjudication(
        record_id=result.record_id,
        route=result.route,
        disposition=str(disposition.value),
        confidence=float(disposition.confidence or 0.0),
        model=result.model,
    )


@activity.defn
async def close_alert(alert_id: str, disposition: str) -> str:
    """The side-effecting act. In a real deployment this writes back to the monitoring system."""
    return f"{alert_id}:{disposition}"


@activity.defn
async def look_up_outcome(alert_id: str) -> str:
    """Ask the system of record what actually happened. The other half of every calibration claim."""
    return current_outcome_source()(alert_id)


@activity.defn
async def record_outcome(decision_record_id: str, label: str) -> str:
    """Link the realised outcome to the decision. A new record; nothing is ever mutated."""
    return current_client().outcome(label=label, decision_record_id=decision_record_id)


# --- what the worker wires in ----------------------------------------------
#
# Activities are module-level functions, so the worker supplies the adapter, the client, the
# question set and the outcome source through this small registry rather than through globals
# scattered across the module.

_WIRING: Dict[str, Any] = {}


def wire(*, adapter: Any, client: Any, questions: Dict[str, Any], outcome_source: Any) -> None:
    _WIRING.update(
        adapter=adapter, client=client, questions=questions, outcome_source=outcome_source
    )


def _require(name: str) -> Any:
    if name not in _WIRING:
        raise RuntimeError(
            f"{name} was never wired; call alert_workflow.wire(...) before starting the worker"
        )
    return _WIRING[name]


def current_adapter() -> Any:
    return _require("adapter")


def current_client() -> Any:
    return _require("client")


def current_questions() -> Dict[str, Any]:
    return _require("questions")


def current_outcome_source() -> Any:
    return _require("outcome_source")
