import asyncio
import concurrent.futures
import uuid
from pathlib import Path

import pytest

pytest.importorskip("celpy")
pytest.importorskip("temporalio")

from warrant import AgentInfo, SQLiteStore, Warrant, verify_records  # noqa: E402
from warrant.adapters import AdapterError, ToolDecision  # noqa: E402
from warrant.adapters.temporal import WarrantInterceptor, model_usage  # noqa: E402
from warrant.ids import deterministic_ulid  # noqa: E402

POLICIES = Path(__file__).parent.parent.parent / "examples" / "policies"
DISBURSE = ToolDecision("credit.approve", subject="loan_id", action="disburse", inputs=["amount", "bureau_score", "foir"], cost_centre="retail-lending")
GOOD = dict(loan_id="LN-1", amount=450000, bureau_score=748, foir=0.38)
BIG = dict(loan_id="LN-2", amount=900000, bureau_score=790, foir=0.30)
WEAK = dict(loan_id="LN-3", amount=100000, bureau_score=610, foir=0.20)


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "records.db"
    w = Warrant("lending", tenant="demo-bank", store=db, agent=AgentInfo("credit-underwriter", "2.4.0"), policy_bundle=POLICIES, currency="INR", flush_interval=0.02)

    def records():
        assert w.flush(10)
        store = SQLiteStore(db, read_only=True)
        try:
            return list(store.iter_records())
        finally:
            store.close()

    yield w, records
    w.close()


def _run(guard, loan, mode, *, review=None, replay_with=None):
    """Run the loan workflow once on Temporal's time-skipping test server and return its result.

    ``review`` is sent as the ``review`` update while the workflow waits for a person.
    ``replay_with`` replays the finished run's history against that workflow class with the guard installed.
    """
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from temporal_app import Loan, LoanWorkflow, disburse, disburse_flaky, disburse_loan, disburse_opaque, underwrite

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            activities = [underwrite, disburse, disburse_flaky, disburse_loan, disburse_opaque, guard.record_activity]
            async with Worker(env.client, task_queue="lending-agents", workflows=[LoanWorkflow], activities=activities,
                              interceptors=[guard], activity_executor=concurrent.futures.ThreadPoolExecutor(2)):
                handle = await env.client.start_workflow(LoanWorkflow.run, args=[Loan(**loan), mode], id=f"loan-{uuid.uuid4()}", task_queue="lending-agents")
                if review is not None:
                    assert await handle.execute_update(LoanWorkflow.review, review) == "noted"
                out = await handle.result()
                if replay_with is not None:
                    await Replayer(workflows=[replay_with], interceptors=[guard]).replay_workflow(await handle.fetch_history())
                return out

    return asyncio.run(go())


def _price(provider, model, tokens_in, tokens_out):
    return tokens_in * 0.001 + tokens_out * 0.005


def test_allowed_activity_runs_and_is_recorded_with_evidence_identity_and_cost(ledger):
    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse": DISBURSE}, pricer=_price), GOOD, "sync")
    assert out == {"result": "disbursed LN-1"}
    (decision,) = records()
    assert decision["decision"] == {"class": "credit.approve", "action": "disburse", "subject": "LN-1", "status": "acted"}
    assert decision["mandate"]["result"] == "allow" and decision["mandate"]["clause"] == "4.2"
    assert [e["name"] for e in decision["evidence"]] == ["temporal.execution", "underwrite", "disburse.result", "anthropic/claude-sonnet-5"]
    identity, underwrite = decision["evidence"][:2]
    assert identity["type"] == "other" and identity["uri"].startswith("temporal://default/loan-") and "?attempt=1&type=disburse&queue=lending-agents" in identity["uri"]
    assert underwrite["type"] == "tool_call" and underwrite["uri"] == "tool://underwrite#1"
    assert decision["cost"] == {"amount": 1.6, "currency": "INR", "cost_centre": "retail-lending",
                                "breakdown": [{"kind": "model_call", "provider": "anthropic", "model": "claude-sonnet-5", "tokens_in": 1200, "tokens_out": 80, "amount": 1.6}]}
    assert all(r.ok for r in verify_records([decision]))


def test_deny_and_escalate_withhold_and_fail_the_activity_without_retry(ledger):
    w, records = ledger
    guard = WarrantInterceptor(w, {"disburse": DISBURSE})
    weak = _run(guard, WEAK, "sync")
    big = _run(guard, BIG, "sync")
    assert weak["blocked"] == "WarrantDenied" and "policy CR-07 clause 4.1 does not allow it" in weak["message"] and "Do not retry" in weak["message"]
    assert big["blocked"] == "WarrantEscalated" and "requires a human to decide" in big["message"]
    by_subject = {r["decision"]["subject"]: r for r in records()}
    assert sorted(by_subject) == ["LN-2", "LN-3"], "one withheld record each: the retry policy did not re-run a blocked activity"
    assert weak["details"]["record_id"] == by_subject["LN-3"]["record_id"] and weak["details"]["clause"] == "4.1"
    assert (by_subject["LN-3"]["decision"]["status"], by_subject["LN-3"]["mandate"]["result"]) == ("withheld", "deny")
    assert by_subject["LN-2"]["mandate"]["result"] == "escalate"
    assert by_subject["LN-2"]["human"] == {"required": True, "note": "awaiting a human's decision; the activity was not run"}
    assert [e["name"] for e in by_subject["LN-2"]["evidence"]] == ["temporal.execution", "underwrite"], "no result: the activity never ran"


def test_unreadable_call_is_blocked_and_is_not_a_decision(ledger):
    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse_opaque": DISBURSE}), GOOD, "opaque")
    assert out["blocked"] == "WarrantUnreadable" and "no subject; expected a non-empty 'loan_id'" in out["message"]
    assert records() == []


def test_failed_attempt_is_recorded_without_error_text_and_the_retry_is_a_new_record(ledger):
    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse_flaky": DISBURSE}), GOOD, "flaky")
    assert out == {"result": "disbursed LN-1"}
    failed, acted = sorted(records(), key=lambda r: r["sequence"])
    assert (failed["decision"]["status"], acted["decision"]["status"]) == ("failed", "acted")
    assert failed["decision"]["summary"] == "ToolCallFailed raised before the action completed" and "ABCDE1234F" not in str(failed)
    assert "?attempt=1&" in failed["evidence"][0]["uri"] and "?attempt=2&" in acted["evidence"][0]["uri"]
    assert failed["record_id"] != acted["record_id"]
    assert [e["name"] for e in failed["evidence"]] == ["temporal.execution", "underwrite"], "a failed attempt does not consume the run's evidence"
    assert [e["name"] for e in acted["evidence"]] == ["temporal.execution", "underwrite", "disburse_flaky.result"]
    assert all(r.ok for r in verify_records([failed, acted]))


def test_record_ids_are_deterministic_so_a_resent_record_is_not_written_twice(ledger, tmp_path):
    w, records = ledger
    _run(WarrantInterceptor(w, {"disburse": DISBURSE}), GOOD, "sync")
    (decision,) = records()
    resent = {k: v for k, v in decision.items() if k not in ("seal", "sequence")}
    store = SQLiteStore(tmp_path / "records.db")
    try:
        store.write([resent])
        assert store.count() == 1
    finally:
        store.close()
    assert deterministic_ulid(1758000000000, "ns|wf|run|3|1") == deterministic_ulid(1758000000000, "ns|wf|run|3|1")
    assert deterministic_ulid(1758000000000, "ns|wf|run|3|1") != deterministic_ulid(1758000000000, "ns|wf|run|3|2")


def test_arguments_are_read_by_name_or_from_a_single_dataclass():
    from dataclasses import dataclass

    from warrant.adapters.temporal import _arguments

    @dataclass
    class Loan:
        loan_id: str
        amount: int

    def by_name(loan_id: str, amount: int) -> None: ...
    def by_dataclass(loan: Loan) -> None: ...

    assert _arguments(by_name, ["LN-1", 5]) == {"loan_id": "LN-1", "amount": 5}
    assert _arguments(by_dataclass, [Loan("LN-1", 5)]) == {"loan_id": "LN-1", "amount": 5}
    with pytest.raises(AdapterError, match="do not match its signature"):
        _arguments(by_name, ["LN-1"])


def test_constructor_and_usage_helper_reject_misuse(ledger):
    w, _ = ledger
    with pytest.raises(ValueError, match="decisions is empty"):
        WarrantInterceptor(w, {})
    with pytest.raises(ValueError, match="max_runs"):
        WarrantInterceptor(w, {"disburse": DISBURSE}, max_runs=0)
    with pytest.raises(RuntimeError, match="inside a Temporal activity"):
        model_usage("anthropic", "claude-sonnet-5")
    with pytest.raises(ValueError, match="26-character ULID"):
        w.decide("credit.approve", subject="LN-1", record_id="not-a-ulid")


# -- workflow side: explicit decisions and approvals ------------------------------


def test_workflow_side_decision_is_recorded_through_the_local_activity_and_replays(ledger):
    from temporal_app import LoanWorkflowPatched

    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse": DISBURSE}), GOOD, "wf", replay_with=LoanWorkflowPatched)
    verdict = out["verdict"]
    assert (verdict["result"], verdict["clause"], verdict["status"]) == ("allow", "4.2", "acted") and "second" not in out
    (decision,) = records()
    assert decision["record_id"] == verdict["record_id"]
    assert decision["decision"] == {"class": "credit.approve", "action": "approve", "subject": "LN-1", "status": "acted", "summary": "workflow-side decision", "alternatives": ["refer", "decline"]}
    (identity,) = decision["evidence"]
    assert identity["name"] == "temporal.execution" and identity["uri"].startswith("temporal://default/loan-") and identity["uri"].count("/") == 4
    assert all(r.ok for r in verify_records([decision]))


def test_workflow_side_deny_is_withheld_and_the_workflow_branches_on_it(ledger):
    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse": DISBURSE}), WEAK, "wf")
    assert (out["verdict"]["result"], out["verdict"]["status"], out["verdict"]["clause"]) == ("deny", "withheld", "4.1")
    (decision,) = records()
    assert decision["decision"]["status"] == "withheld" and decision["decision"]["action"] == "none"


def test_escalation_approved_by_a_person_runs_the_activity_and_links_the_verdict(ledger):
    w, records = ledger
    out = _run(WarrantInterceptor(w, {"disburse_loan": DISBURSE}), BIG, "approve", review={"approve": True, "reviewer": "asha@bank.example", "note": "DSCR checked"})
    assert out["result"] == "disbursed LN-2"
    withheld, verdict, acted = sorted(records(), key=lambda r: r["sequence"])
    assert withheld["record_id"] == out["escalated"] and withheld["decision"]["status"] == "withheld" and withheld["human"]["required"] is True
    assert verdict["record_type"] == "human_verdict" and verdict["references"] == {"decision_record_id": withheld["record_id"]}
    assert verdict["human"]["reviewer"] == "asha@bank.example" and verdict["human"]["verdict"] == "approve" and verdict["human"]["note"] == "DSCR checked"
    assert acted["decision"]["status"] == "acted" and acted["mandate"]["result"] == "escalate"
    assert acted["human"] == {"required": True, "reviewer": "asha@bank.example", "note": f"approved after escalation {withheld['record_id']}"}
    assert [e["name"] for e in withheld["evidence"]] == ["temporal.execution", "underwrite"], "the escalation took the run's evidence"
    assert [e["name"] for e in acted["evidence"]] == ["temporal.execution", "warrant.escalation", "disburse_loan.result"]
    assert acted["evidence"][1]["uri"] == f"warrant://record/{withheld['record_id']}"
    assert all(r.ok for r in verify_records([withheld, verdict, acted]))


def test_escalation_rejected_or_timed_out_records_the_verdict_and_runs_nothing(ledger):
    w, records = ledger
    guard = WarrantInterceptor(w, {"disburse_loan": DISBURSE})
    rejected = _run(guard, BIG, "approve", review={"approve": False, "reviewer": "asha@bank.example", "note": "income unverified"})
    timed_out = _run(guard, {**BIG, "loan_id": "LN-4"}, "approve")
    assert rejected["verdict"]["verdict"] == "reject" and timed_out["timed_out"] is True
    by_type = {}
    for r in records():
        by_type.setdefault(r["record_type"], []).append(r)
    assert sorted(by_type) == ["decision", "human_verdict"] and len(by_type["decision"]) == 2, "no acted record: nothing ran"
    notes = sorted((v["human"]["reviewer"], v["human"]["note"]) for v in by_type["human_verdict"])
    assert notes == [("asha@bank.example", "income unverified"), ("system", "timed out")]
    assert {v["references"]["decision_record_id"] for v in by_type["human_verdict"]} == {rejected["escalated"], timed_out["escalated"]}


def test_a_denial_is_not_an_escalation():
    from temporalio.exceptions import ApplicationError

    from warrant.adapters.temporal_workflow import blocked, escalation

    denied = ApplicationError("no", {"record_id": "X", "result": "deny"}, type="WarrantDenied", non_retryable=True)
    assert blocked(denied) == {"record_id": "X", "result": "deny"} and escalation(denied) is None
    assert blocked(RuntimeError("other")) is None
