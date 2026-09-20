"""The three layers end to end: Temporal endures, a model decides, Warrant proves.

Runs against Temporal's time-skipping test server, which is what makes the ninety-day outcome
timer testable at all — the workflow really waits out the horizon, in a few milliseconds of wall
clock. The decision model is a fake: the value being proved is the composition, not that a vendor's
servers answer, and a live call would need the user's key and spend their credits.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from warrant import AgentInfo, SQLiteStore, Warrant
from warrant.adapters.base import DecisionModel, ModelAnswer, ModelResult
from warrant.adapters.model import DecisionAdapter

pytest.importorskip("temporalio", reason="the platform example needs the temporal extra")

from temporalio.client import Client  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from warrant.adapters.temporal import WarrantInterceptor  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "platform"))
import alert_workflow as app  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="temporalio needs 3.10 or newer"
)


class _ScriptedModel(DecisionModel):
    """Answers from a script, so a test can put a decision in any confidence band it likes."""

    provider = "scripted"
    endpoint = "https://scripted.invalid"
    region = "global"

    def __init__(self, disposition: str, confidence: float) -> None:
        self.disposition = disposition
        self.confidence = confidence
        self.calls = 0

    def evaluate(self, state, questions):
        self.calls += 1
        return ModelResult(
            answers={
                "disposition": ModelAnswer(
                    "disposition",
                    self.disposition,
                    self.confidence,
                    {self.disposition: self.confidence, "escalate": round(1 - self.confidence, 4)},
                )
            },
            model="scripted-1",
            tokens_in=1800,
            tokens_out=0,
        )


POLICIES = Path(__file__).resolve().parents[2] / "examples" / "gallery" / "aml" / "policies"

ALERT = app.Alert(
    alert_id="TM-70001",
    segment="retail",
    profile_consistent=True,
    structuring_pattern=False,
    counterparty_risk=0.1,
    explanation_on_file=True,
    behaviour_change=0.1,
    amount_band="1l_5l",
)


async def _run(env, tmp_path, model, *, outcome="stayed_closed", review=None, alert=ALERT):
    """Start the worker, run one alert through, and return (result, records)."""
    store = tmp_path / "records.db"
    client = Warrant(
        "aml",
        tenant="demo-bank",
        store=store,
        agent=AgentInfo("alert-adjudicator", "1.4.0"),
        policy_bundle=POLICIES,
        currency="INR",
        flush_interval=0.02,
    )
    adapter = DecisionAdapter(client, model)
    app.wire(
        adapter=adapter,
        client=client,
        questions={"disposition": object()},
        outcome_source=lambda alert_id: outcome,
    )
    interceptor = WarrantInterceptor(client, {}, workflow_only=True)
    try:
        async with Worker(
            env.client,
            task_queue="platform-test",
            workflows=[app.AlertAdjudication],
            activities=[
                app.adjudicate_alert,
                app.close_alert,
                app.look_up_outcome,
                app.record_outcome,
                interceptor.record_activity,
            ],
            interceptors=[interceptor],
        ):
            handle = await env.client.start_workflow(
                app.AlertAdjudication.run,
                alert,
                id=f"alert-{alert.alert_id}",
                task_queue="platform-test",
            )
            if review is not None:
                await handle.signal(
                    app.AlertAdjudication.reviewed,
                    args=[review["reviewer"], review["verdict"], review.get("note", "")],
                )
            result = await handle.result()
        assert client.flush(timeout=5)
        records = list(SQLiteStore(store, read_only=True).iter_records("aml"))
    finally:
        client.close()
    return result, records


def _by_type(records, kind):
    return [r for r in records if r["record_type"] == kind]


def test_a_confident_alert_is_auto_closed_and_its_outcome_arrives_ninety_days_later(tmp_path):
    asyncio.run(_test_a_confident_alert_is_auto_closed_and_its_outcome_arrives_ninety_days_later(tmp_path))


async def _test_a_confident_alert_is_auto_closed_and_its_outcome_arrives_ninety_days_later(tmp_path):
    """The whole loop: decide, act, wait out the horizon, link what actually happened."""
    model = _ScriptedModel("close", 0.95)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, records = await _run(env, tmp_path, model)

    assert result.route == "auto"
    assert result.outcome == "stayed_closed"
    assert "closed automatically" in result.history

    decisions = _by_type(records, "decision")
    assert len(decisions) == 1  # exactly one record for one decision
    decision = decisions[0]["decision"]
    assert decision["route"] == "auto"
    assert decision["question_set"] == {"id": "aml.alert", "version": "3.1.0"}
    assert decision["answers"][0]["confidence"] == 0.95
    assert decision["state_digest"]

    # the Temporal execution rode along without the application passing anything
    evidence = {e["name"]: e["uri"] for e in decisions[0]["evidence"]}
    assert evidence["temporal.execution"].startswith("temporal://")
    assert "attempt=1" in evidence["temporal.execution"]

    outcomes = _by_type(records, "outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"]["label"] == "stayed_closed"
    assert outcomes[0]["references"]["decision_record_id"] == decisions[0]["record_id"]


def test_a_carve_out_segment_is_never_auto_closed_however_confident(tmp_path):
    asyncio.run(_test_a_carve_out_segment_is_never_auto_closed_however_confident(tmp_path))


async def _test_a_carve_out_segment_is_never_auto_closed_however_confident(tmp_path):
    """AML-01 clause 2.1: a politically exposed person goes to a human at any confidence."""
    pep = app.Alert(**{**ALERT.__dict__, "alert_id": "TM-70002", "segment": "pep"})
    model = _ScriptedModel("close", 0.99)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, records = await _run(
            env, tmp_path, model, alert=pep,
            review={"reviewer": "mlro@bank.example", "verdict": "approve", "note": "checked"},
        )

    assert result.route == "human"
    assert result.reviewer == "mlro@bank.example"
    decision = _by_type(records, "decision")[0]
    assert decision["decision"]["route"] == "human"
    assert decision["human"]["required"] is True
    assert decision["mandate"]["clause"] == "2.1"

    verdicts = _by_type(records, "human_verdict")
    assert len(verdicts) == 1
    assert verdicts[0]["human"]["verdict"] == "approve"
    assert verdicts[0]["human"]["reviewer"] == "mlro@bank.example"
    assert verdicts[0]["references"]["decision_record_id"] == decision["record_id"]


def test_a_rejected_review_keeps_the_alert_open_and_records_the_rejection(tmp_path):
    asyncio.run(_test_a_rejected_review_keeps_the_alert_open_and_records_the_rejection(tmp_path))


async def _test_a_rejected_review_keeps_the_alert_open_and_records_the_rejection(tmp_path):
    model = _ScriptedModel("close", 0.5)  # below the 0.90 floor, so it routes to a person
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result, records = await _run(
            env, tmp_path, model, outcome="reopened",
            review={"reviewer": "l2@bank.example", "verdict": "reject", "note": "structuring"},
        )

    assert result.route == "human"
    assert "kept open after review" in result.history
    assert _by_type(records, "human_verdict")[0]["human"]["verdict"] == "reject"
    # the outcome is still attached: a decision a person made is still a decision to calibrate
    assert _by_type(records, "outcome")[0]["outcome"]["label"] == "reopened"


def test_the_model_is_called_once_and_the_record_is_not_duplicated(tmp_path):
    asyncio.run(_test_the_model_is_called_once_and_the_record_is_not_duplicated(tmp_path))


async def _test_the_model_is_called_once_and_the_record_is_not_duplicated(tmp_path):
    """The composition rule: the model adapter owns the record, the Temporal mapping does not.

    If `adjudicate_alert` were also listed as a ToolDecision, the same decision would be recorded
    twice. The interceptor here carries an empty mapping on purpose.
    """
    model = _ScriptedModel("close", 0.95)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, records = await _run(env, tmp_path, model)
    assert model.calls == 1
    assert len(_by_type(records, "decision")) == 1


def test_the_chain_verifies_after_the_whole_run(tmp_path):
    asyncio.run(_test_the_chain_verifies_after_the_whole_run(tmp_path))


async def _test_the_chain_verifies_after_the_whole_run(tmp_path):
    from warrant.verify import verify_records

    model = _ScriptedModel("close", 0.95)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        _, records = await _run(env, tmp_path, model)
    reports = verify_records(records)
    assert all(r.ok for r in reports), [r.errors for r in reports]


def test_an_outcome_signalled_early_short_circuits_the_horizon(tmp_path):
    asyncio.run(_test_an_outcome_signalled_early_short_circuits_the_horizon(tmp_path))


async def _test_an_outcome_signalled_early_short_circuits_the_horizon(tmp_path):
    """A bank that already knows the answer should not wait ninety days to say so."""
    model = _ScriptedModel("close", 0.95)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        store = tmp_path / "records.db"
        client = Warrant(
            "aml", tenant="demo-bank", store=store,
            agent=AgentInfo("alert-adjudicator", "1.4.0"),
            policy_bundle=POLICIES, currency="INR", flush_interval=0.02,
        )
        app.wire(
            adapter=DecisionAdapter(client, model), client=client,
            questions={"disposition": object()},
            outcome_source=lambda alert_id: pytest.fail("the horizon should not have been reached"),
        )
        interceptor = WarrantInterceptor(client, {}, workflow_only=True)
        try:
            async with Worker(
                env.client, task_queue="platform-early",
                workflows=[app.AlertAdjudication],
                activities=[app.adjudicate_alert, app.close_alert, app.look_up_outcome,
                            app.record_outcome, interceptor.record_activity],
                interceptors=[interceptor],
            ):
                handle = await env.client.start_workflow(
                    app.AlertAdjudication.run, ALERT, id="alert-early", task_queue="platform-early"
                )
                await handle.signal(app.AlertAdjudication.outcome_observed, "reopened")
                result = await handle.result()
            assert client.flush(timeout=5)
        finally:
            client.close()
    assert result.outcome == "reopened"
