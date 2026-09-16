import threading

import pytest

from warrant import Decision, Redactor, Verdict, Warrant, current_decision, validate
from warrant.verify import verify_records


def _records(client, record_type=None):
    assert client.flush(timeout=5)
    items = list(client.store.iter_records("lending"))
    return [r for r in items if record_type is None or r["record_type"] == record_type]


def test_happy_path_record_has_every_section(client):
    with client.decide("credit.approve", subject="LN-20431", on_behalf_of="branch:jayanagar", alternatives=["refer", "decline"]) as d:
        verdict = d.check(amount=450000, bureau_score=748)
        assert verdict.result == "unchecked" and not verdict.allowed
        digest = d.evidence("bureau_pull", uri="cibil://req/88213", type="tool_call", content={"score": 748}, excerpt="score 748")
        d.model_call("anthropic", "claude-sonnet-5", tokens_in=6120, tokens_out=410, amount=2.11)
        d.tool_call("kyc_match", uri="kyc://match/LN-20431", content=b"0.97", amount=0.15, excerpt="match 0.97")
        d.act("approve", summary="Approve personal loan LN-20431", cost_centre="retail-lending")
        record_id = d.record_id

    (record,) = _records(client, "decision")
    validate(record)
    assert record["record_id"] == record_id
    assert record["origin"] == "live" and record["tenant"] == "demo-bank"
    assert record["actor"] == {"name": "credit-underwriter", "version": "2.3.1", "instance": "test", "on_behalf_of": "branch:jayanagar"}
    assert record["decision"] == {
        "class": "credit.approve", "action": "approve", "subject": "LN-20431", "status": "acted",
        "summary": "Approve personal loan LN-20431", "alternatives": ["refer", "decline"],
    }
    assert record["mandate"] == {"result": "unchecked"}
    names = [e["name"] for e in record["evidence"]]
    assert names == ["bureau_pull", "anthropic/claude-sonnet-5", "kyc_match"]
    assert record["evidence"][0]["content_hash"] == digest
    assert "content" not in record["evidence"][0]
    assert record["cost"]["currency"] == "INR" and record["cost"]["amount"] == pytest.approx(2.26)
    assert record["cost"]["cost_centre"] == "retail-lending"
    assert [c["kind"] for c in record["cost"]["breakdown"]] == ["model_call", "tool_call"]
    assert record["human"] == {"required": False}
    assert record["outcome"] == {"status": "pending"}
    assert record["sequence"] == 1 and record["seal"]["prev_hash"] is None


def test_exception_inside_scope_records_failure_and_reraises(client):
    with pytest.raises(RuntimeError, match="boom"):
        with client.decide("credit.approve", subject="LN-1") as d:
            d.evidence("x", uri="doc://1", content="abc")
            raise RuntimeError("boom secret-value")

    (record,) = _records(client, "decision")
    assert record["decision"]["status"] == "failed"
    assert record["decision"]["action"] == "none"
    assert "secret-value" not in record["decision"]["summary"]
    assert record["decision"]["summary"].startswith("RuntimeError")
    assert len(record["evidence"]) == 1


def test_scope_without_act_is_withheld(client):
    with client.decide("credit.approve", subject="LN-2") as d:
        d.check(amount=1)
    (record,) = _records(client, "decision")
    assert record["decision"]["status"] == "withheld"
    assert record["decision"]["action"] == "none"
    assert "unchecked" in record["decision"]["summary"]


def test_policy_engine_verdict_lands_in_mandate(tmp_path, agent):
    class Engine:
        def evaluate(self, decision_class, inputs):
            assert decision_class == "credit.approve"
            if inputs["amount"] <= 500000:
                return Verdict("allow", policy_id="CR-07", policy_version="2026.3", clause="4.2")
            return Verdict("deny", policy_id="CR-07", policy_version="2026.3", clause="4.1", reason="over limit")

    with Warrant("lending", store=tmp_path / "r.db", agent=agent, policy=Engine(), flush_interval=0.05) as w:
        with w.decide("credit.approve", subject="A") as d:
            assert d.check(amount=450000).allowed
            d.act("approve")
        with w.decide("credit.approve", subject="B") as d:
            v = d.check(amount=900000)
            assert not v.allowed and v.result == "deny"
        assert w.flush()
        a, b = list(w.store.iter_records("lending"))
    assert a["mandate"] == {"result": "allow", "policy_id": "CR-07", "policy_version": "2026.3", "clause": "4.2"}
    assert b["mandate"]["result"] == "deny" and b["decision"]["status"] == "withheld"


def test_input_validation_at_boundaries(client):
    with pytest.raises(ValueError, match="decision class"):
        client.decide("Credit Approve", subject="x")
    with pytest.raises(ValueError, match="subject"):
        client.decide("credit.approve", subject="")
    with client.decide("credit.approve", subject="x") as d:
        with pytest.raises(ValueError, match="content"):
            d.evidence("e", uri="doc://1")
        with pytest.raises(ValueError, match="content_hash"):
            d.evidence("e", uri="doc://1", content_hash="nothex")
        with pytest.raises(ValueError, match="evidence type"):
            d.evidence("e", uri="doc://1", content="x", type="rumour")
        with pytest.raises(ValueError, match="non-negative"):
            d.cost(-1)
        with pytest.raises(ValueError, match="tokens_in"):
            d.cost(1, kind="model_call", tokens_in=-5)
        d.act("approve")
        with pytest.raises(RuntimeError, match="already called"):
            d.act("approve")
    with pytest.raises(RuntimeError, match="closed"):
        d.act("late")
    with pytest.raises(ValueError, match="currency"):
        Warrant("s", store=client.store.path, currency="rupees")


def test_current_decision_is_scoped(client):
    assert current_decision() is None
    with client.decide("credit.approve", subject="outer") as outer:
        assert current_decision() is outer
        with client.decide("credit.approve", subject="inner") as inner:
            assert current_decision() is inner
            inner.act("x")
        assert current_decision() is outer
        outer.act("y")
    assert current_decision() is None
    assert len(_records(client, "decision")) == 2


def test_redaction_applies_before_emit(tmp_path, agent):
    redactor = Redactor(patterns=[r"\b\d{4}\s?\d{4}\s?\d{4}\b"], fields=["actor.on_behalf_of"])
    with Warrant("lending", store=tmp_path / "r.db", agent=agent, redact=redactor, flush_interval=0.05) as w:
        with w.decide("kyc.approve", subject="C-1", on_behalf_of="officer:asha") as d:
            d.evidence("aadhaar", uri="kyc://doc/1", content="raw", excerpt="Aadhaar 1234 5678 9012 matched")
            d.act("approve", summary="Approved customer with Aadhaar 1234 5678 9012")
        assert w.flush()
        (record,) = list(w.store.iter_records("lending"))
    assert record["decision"]["summary"] == "Approved customer with Aadhaar [REDACTED]"
    assert record["evidence"][0]["excerpt"] == "Aadhaar [REDACTED] matched"
    assert record["actor"]["on_behalf_of"] == "[REDACTED]"
    assert record["evidence"][0]["uri"] == "kyc://doc/1"


def test_outcome_and_human_verdict_link_to_decision(client):
    with client.decide("credit.approve", subject="LN-9") as d:
        d.require_human(reviewer="risk-desk")
        d.act("approve")
    oid = client.outcome(subject="LN-9", label="performing", observed_at="2026-12-15T00:00:00Z", score=0.9, source="lms://LN-9")
    hid = client.human_verdict(subject="LN-9", reviewer="asha", verdict="approve", note="sampled")
    decision, outcome, human = _records(client)
    assert decision["human"] == {"required": True, "reviewer": "risk-desk"}
    assert outcome["record_id"] == oid and outcome["references"]["decision_record_id"] == decision["record_id"]
    assert outcome["outcome"] == {"status": "observed", "label": "performing", "observed_at": "2026-12-15T00:00:00Z", "score": 0.9, "source": "lms://LN-9"}
    assert human["record_id"] == hid and human["human"]["verdict"] == "approve"
    assert [r["sequence"] for r in (decision, outcome, human)] == [1, 2, 3]
    assert outcome["seal"]["prev_hash"] == decision["seal"]["hash"]


def test_outcome_for_unknown_subject_raises(client):
    with pytest.raises(LookupError, match="no decision for subject"):
        client.outcome(subject="nope", label="x")
    with pytest.raises(ValueError, match="subject or decision_record_id"):
        client.outcome(label="x")


def test_concurrent_decisions_keep_a_valid_chain(client):
    def worker(n):
        for i in range(25):
            with client.decide("invoice.approve", subject=f"INV-{n}-{i}") as d:
                d.cost(0.01)
                d.act("approve")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = _records(client)
    assert len(records) == 100
    assert [r["sequence"] for r in records] == list(range(1, 101))
    (report,) = verify_records(records)
    assert report.ok, report.errors
    assert client.stats()["delivered"] == 100 and client.stats()["spilled"] == 0


def test_default_store_path_from_env(tmp_path, monkeypatch, agent):
    monkeypatch.setenv("WARRANT_STORE", str(tmp_path / "env.db"))
    with Warrant("s", agent=agent) as w:
        assert w.store.path == tmp_path / "env.db"
