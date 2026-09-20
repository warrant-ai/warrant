import json
import os
import textwrap
import xml.etree.ElementTree as ET

import pytest

from warrant import AgentInfo, Unreplayable, Warrant
from warrant.cli import main
from warrant.replay import FrozenSource, Target, load_decider, load_targets, parse_agent, replay, save_targets
from warrant.sets import DecisionSet, SetItem, build_set

AGENT = AgentInfo("credit-underwriter", "2.3.1")


def bureau(score):
    return {"score": score, "enquiries": 1}


def decide_v1(d, inputs, target):
    """The recorded behaviour: approve when score >= 720, using a bureau tool and one model call."""
    d.check(**inputs)
    pulled = d.tool("bureau_pull", lambda: bureau(inputs["bureau_score"]), uri=f"cibil://{inputs['subject']}")
    d.model_call("anthropic", "claude-sonnet-5", tokens_in=1000, tokens_out=100, amount=2.0)
    if pulled["score"] >= 720:
        d.act("approve", summary="within policy")
    else:
        d.act("refer", summary="below threshold")


def decide_v2(d, inputs, target):
    """Changed threshold and cheaper model: 3 of the recorded decisions flip."""
    d.check(**inputs)
    pulled = d.tool("bureau_pull", lambda: (_ for _ in ()).throw(AssertionError("tool must not run in frozen mode")))
    model = target.model or "anthropic/claude-haiku-4-5-20251001"
    d.model_call(*model.split("/", 1), tokens_in=1000, tokens_out=100, amount=0.5)
    if pulled["score"] >= 750:
        d.act("approve")
    else:
        d.act("refer")


def decide_new_tool(d, inputs, target):
    d.check(**inputs)
    d.tool("fraud_check", lambda: {"risk": "low"})
    d.act("approve")


def decide_raises(d, inputs, target):
    raise KeyError("prompt template missing")


@pytest.fixture
def recorded(tmp_path):
    """Six recorded decisions with inputs and captured evidence, three with outcomes."""
    db = tmp_path / "records.db"
    with Warrant("lending", store=db, agent=AGENT, currency="INR", capture_inputs=True, capture_evidence=True, flush_interval=0.02) as w:
        scores = {"LN-1": 800, "LN-2": 760, "LN-3": 740, "LN-4": 725, "LN-5": 700, "LN-6": 650}
        for subject, score in scores.items():
            with w.decide("credit.approve", subject=subject) as d:
                decide_v1(d, {"subject": subject, "bureau_score": score, "amount": 100000}, None)
        w.outcome(subject="LN-1", label="performing")
        w.outcome(subject="LN-3", label="default")
        w.outcome(subject="LN-4", label="default")
        assert w.flush()
    return db


def test_recording_with_capture_stores_inputs_and_evidence_content(recorded):
    from warrant import SQLiteStore

    store = SQLiteStore(recorded, read_only=True)
    decisions = [r for r in store.iter_records("lending") if r["record_type"] == "decision"]
    assert decisions[0]["decision"]["inputs"] == {"subject": "LN-1", "bureau_score": 800, "amount": 100000}
    tool_ev = [e for e in decisions[0]["evidence"] if e["type"] == "tool_call"][0]
    blob = store.get_blob(tool_ev["content_hash"])
    assert blob == {"encoding": "json", "data": {"score": 800, "enquiries": 1}}
    assert "_blobs" not in decisions[0]
    assert store.latest_outcome(decisions[2]["record_id"])["outcome"]["label"] == "default"
    store.close()


def test_build_set_joins_outcomes_filters_and_roundtrips(recorded, tmp_path):
    from warrant import SQLiteStore

    store = SQLiteStore(recorded, read_only=True)
    full = build_set(store, "all", stream="lending")
    assert len(full) == 6 and full.items[0].outcome_label == "performing" and full.items[1].outcome_label is None
    assert all(item.evidence_content for item in full)
    limited = build_set(store, "two", stream="lending", limit=2)
    assert [i.subject for i in limited] == ["LN-1", "LN-2"]
    picked = build_set(store, "picked", stream="lending", subjects=["LN-5", "LN-6"])
    assert [i.subject for i in picked] == ["LN-5", "LN-6"]
    pytest.importorskip("celpy")
    bad = build_set(store, "bad", stream="lending", where="outcome.label == 'default'")
    assert [i.subject for i in bad] == ["LN-3", "LN-4"]
    cheap_refers = build_set(store, "x", stream="lending", where="decision.action == 'refer' && cost.amount < 3")
    assert [i.subject for i in cheap_refers] == ["LN-5", "LN-6"]
    with pytest.raises(ValueError, match="invalid --where"):
        build_set(store, "x", stream="lending", where="outcome.label ==")
    store.close()

    path = full.save(tmp_path / "sets" / "all.jsonl")
    loaded = DecisionSet.load(path)
    assert loaded.name == "all" and len(loaded) == 6 and loaded.source["stream"] == "lending"
    assert loaded.items[0].record == full.items[0].record and loaded.items[0].evidence_content == full.items[0].evidence_content
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        DecisionSet.load(tmp_path / "empty.jsonl")


def test_frozen_source_serves_in_order_and_reports_missing():
    record = {"evidence": [
        {"name": "t", "type": "tool_call", "content_hash": "a" * 64},
        {"name": "t", "type": "tool_call", "content_hash": "b" * 64},
        {"name": "gone", "type": "tool_call", "content_hash": "c" * 64},
        {"name": "m", "type": "model_call", "content_hash": "d" * 64},
    ]}
    src = FrozenSource(SetItem(record, {"a" * 64: {"encoding": "json", "data": 1}, "b" * 64: {"encoding": "utf8", "data": "two"}}))
    assert src.lookup("t") == 1 and src.lookup("t") == "two"
    with pytest.raises(Unreplayable, match="was not called"):
        src.lookup("t")
    with pytest.raises(Unreplayable, match="recorded by hash only"):
        src.lookup("gone")
    with pytest.raises(Unreplayable, match="was not called"):
        src.lookup("never")


def _set_from(recorded):
    from warrant import SQLiteStore

    store = SQLiteStore(recorded, read_only=True)
    ds = build_set(store, "lending-all", stream="lending")
    store.close()
    return ds


def test_frozen_replay_diffs_flips_cost_and_outcomes(recorded):
    ds = _set_from(recorded)
    target = Target("v2", AgentInfo("credit-underwriter", "2.4.0"), model="anthropic/claude-haiku-4-5-20251001")
    report = replay(ds, target, decide_v2, mode="frozen", fail_on=["flipped", "unreplayable", "errored"], max_cost_increase=10)
    assert report.total == 6 and len(report.replayed) == 6
    assert sorted(r.subject for r in report.flipped) == ["LN-3", "LN-4"]  # 740 and 725 drop below the new 750 bar
    assert report.flip_transitions() == {"approve -> refer": 2}
    assert report.flips_by_outcome() == {"default": 2}
    assert report.cost_before == pytest.approx(12.0) and report.cost_after == pytest.approx(3.0)
    assert report.cost_change_pct == -75.0
    assert report.failures() == ["2 flipped decision(s) exceed threshold 0"]
    assert not report.passed
    text = report.summary("INR ")
    assert "# 6 decisions replayed (frozen) against v2" in text
    assert "# 2 flipped (2 approve -> refer)" in text and "by recorded outcome: 2 default" in text
    assert "cost INR 2.00 -> INR 0.50 per decision (-75%)" in text and text.endswith("# FAILED: 2 flipped decision(s) exceed threshold 0")
    first = report.replayed[0].after_record
    assert first["actor"] == {"name": "credit-underwriter", "version": "2.4.0"} and first["cost"]["currency"] == "INR"
    assert first["evidence"][0]["type"] == "tool_call" and first["evidence"][0]["content_hash"] == ds.items[0].record["evidence"][0]["content_hash"]


def test_replay_reports_unreplayable_errored_and_missing_inputs(recorded):
    ds = _set_from(recorded)
    target = Target("v3", AgentInfo("a", "3"))
    report = replay(ds, target, decide_new_tool, fail_on=["unreplayable"])
    assert len(report.unreplayable) == 6 and "fraud_check" in report.unreplayable[0].detail
    assert report.failures() == ["6 unreplayable decision(s)"] and "# 6 unreplayable" in report.summary()

    report = replay(ds, target, decide_raises)  # no gates requested, errors still fail
    assert len(report.errored) == 6 and report.errored[0].detail.startswith("KeyError")
    assert report.cost_change_pct is None and not report.passed
    assert report.failures() == ["6 decision(s) raised in the decider"]
    root = ET.fromstring(report.to_junit())
    assert root.get("failures") == "6"

    for item in ds.items:
        item.record["decision"].pop("inputs")
    report = replay(ds, target, decide_v2)
    assert all(r.status == "unreplayable" and "capture_inputs" in r.detail for r in report.results)
    assert report.passed  # no gates requested


def test_live_mode_calls_tools_and_policy_bundle_applies(recorded, tmp_path):
    pytest.importorskip("celpy")
    ds = _set_from(recorded)
    calls = []

    def decide_live(d, inputs, target):
        v = d.check(amount=inputs["amount"], bureau_score=inputs["bureau_score"], foir=0.3)
        d.tool("bureau_pull", lambda: calls.append(inputs["subject"]) or bureau(inputs["bureau_score"]))
        d.act("approve" if v.allowed else "refer")

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "cr.yaml").write_text(textwrap.dedent("""
        policy_id: CR-07
        version: "2026.4"
        classes: [credit.approve]
        default: deny
        clauses:
          - {id: "1", when: "bureau_score >= 760", result: allow}
    """), encoding="utf-8")
    target = Target("strict", AgentInfo("a", "1"), policy=str(policies))
    report = replay(ds, target, decide_live, mode="live", fail_on=["new-deny"], concurrency=3)
    assert sorted(calls) == ["LN-1", "LN-2", "LN-3", "LN-4", "LN-5", "LN-6"]
    assert sorted(r.subject for r in report.new_denies) == ["LN-3", "LN-4", "LN-5", "LN-6"]
    assert "# 4 new deny (CR-07 default)" in report.summary()
    assert report.failures() == ["4 new mandate denial(s)"]


def test_report_json_and_junit(recorded, tmp_path):
    ds = _set_from(recorded)
    report = replay(ds, Target("v2", AgentInfo("a", "2")), decide_v2, fail_on=["flipped"], max_cost_increase=-90)
    data = report.to_dict()
    # model_drift is 6 on purpose: this target names no model, so decide_v2 falls back to haiku
    # and every decision was answered by a model the target never asked for. That is exactly the
    # signal a golden run watches for, and it is a real one here rather than fixture noise.
    assert data["totals"] == {
        "replayed": 6, "flipped": 2, "new_deny": 0, "new_escalate": 0, "unreplayable": 0, "errored": 0,
        "answer_drift": 0, "confidence_drift": 0, "model_drift": 6,
        "cost_before": 12.0, "cost_after": 3.0, "cost_change_pct": -75.0,
    }
    assert data["gates"]["passed"] is False and len(data["gates"]["failures"]) == 2
    assert data["results"][2]["flipped"] is True and data["results"][2]["cost_delta"] == -1.5 and "after_record" not in data["results"][2]
    json.dumps(data)
    root = ET.fromstring(report.to_junit())
    assert root.tag == "testsuite" and root.get("tests") == "7" and root.get("failures") == "3"
    failed = [c.get("name") for c in root if c.find("failure") is not None]
    assert failed == ["credit.approve LN-3", "credit.approve LN-4", "cost budget"]


def test_replay_argument_validation(recorded):
    ds = _set_from(recorded)
    t = Target("t", AgentInfo("a", "1"))
    with pytest.raises(ValueError, match="mode"):
        replay(ds, t, decide_v2, mode="warm")
    with pytest.raises(ValueError, match="unknown fail-on gate"):
        replay(ds, t, decide_v2, fail_on=["flips"])
    with pytest.raises(ValueError, match="concurrency"):
        replay(ds, t, decide_v2, concurrency=0)


def test_targets_and_helpers(tmp_path):
    assert parse_agent("credit-underwriter@2.4.0") == AgentInfo("credit-underwriter", "2.4.0")
    with pytest.raises(ValueError):
        parse_agent("no-version")
    path = tmp_path / "targets.json"
    assert load_targets(path) == {}
    save_targets(path, {"v2": Target("v2", AgentInfo("a", "2"), model="m", policy="p", decider="mod:fn")})
    loaded = load_targets(path)
    assert loaded["v2"].model == "m" and loaded["v2"].decider == "mod:fn" and loaded["v2"].agent.version == "2"
    path.write_text('{"broken": {"model": "m"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="agent name and version"):
        load_targets(path)
    with pytest.raises(ValueError, match="module:function"):
        load_decider("nocolon")
    with pytest.raises(ImportError):
        load_decider("definitely_missing_module_xyz:fn")
    with pytest.raises(ValueError, match="not a callable"):
        load_decider("json:__doc__")


def test_cli_end_to_end(recorded, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "underwriter.py").write_text(textwrap.dedent("""
        def decide(d, inputs, target):
            d.check(**inputs)
            pulled = d.tool("bureau_pull", lambda: None)
            d.model_call("anthropic", "claude-haiku-4-5-20251001", tokens_in=1000, tokens_out=100, amount=0.5)
            d.act("approve" if pulled["score"] >= 750 else "refer")
    """), encoding="utf-8")
    ws = str(tmp_path / ".warrant")
    assert main(["--workspace", ws, "set", "create", "lending-all", "--store", str(recorded), "--from-stream", "lending"]) == 0
    out = capsys.readouterr().out
    assert "set lending-all: 6 decision(s)" in out and "6 with recorded inputs, 6 with captured evidence content" in out
    assert main(["--workspace", ws, "set", "list"]) == 0
    assert "lending-all: 6 decision(s)" in capsys.readouterr().out
    assert main(["--workspace", ws, "target", "add", "underwriter-v2.4", "--agent", "credit-underwriter@2.4.0", "--model", "anthropic/claude-haiku-4-5-20251001", "--decider", "underwriter:decide"]) == 0
    assert "target underwriter-v2.4: credit-underwriter@2.4.0" in capsys.readouterr().out
    assert main(["--workspace", ws, "target", "list"]) == 0
    capsys.readouterr()

    j, x = str(tmp_path / "r.json"), str(tmp_path / "r.xml")
    code = main(["--workspace", ws, "test", "lending-all", "--against", "underwriter-v2.4", "--mode", "frozen", "--fail-on", "flipped,new-deny", "--max-cost-increase", "10%", "--json", j, "--junit", x])
    out = capsys.readouterr().out
    assert code == 1
    assert "# 6 decisions replayed (frozen) against underwriter-v2.4" in out
    assert "# 2 flipped (2 approve -> refer)" in out and "# FAILED: 2 flipped decision(s) exceed threshold 0" in out
    assert json.load(open(j))["totals"]["flipped"] == 2 and os.path.getsize(x) > 0

    assert main(["--workspace", ws, "test", "lending-all", "--against", "underwriter-v2.4", "--max-cost-increase", "10%"]) == 0
    assert "# PASSED" in capsys.readouterr().out

    assert main(["--workspace", ws, "test", "nope", "--against", "underwriter-v2.4"]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["--workspace", ws, "test", "lending-all", "--against", "ghost"]) == 1
    assert "known: underwriter-v2.4" in capsys.readouterr().err
    assert main(["--workspace", ws, "target", "add", "bare", "--agent", "a@1"]) == 0
    capsys.readouterr()
    assert main(["--workspace", ws, "test", "lending-all", "--against", "bare"]) == 2
    assert "no decider" in capsys.readouterr().err
    assert main(["--workspace", ws, "test", "lending-all", "--against", "bare", "--decider", "underwriter:nope"]) == 2
    assert "not a callable" in capsys.readouterr().err
    assert main(["--workspace", ws, "set", "create", "none", "--store", str(recorded), "--from-stream", "empty"]) == 1
    assert "no decisions matched" in capsys.readouterr().err
    assert main(["--workspace", ws, "set", "create", "x", "--store", str(tmp_path / "missing.db"), "--from-stream", "lending"]) == 1
    assert main(["--workspace", ws, "target", "add", "bad", "--agent", "noversion"]) == 2
    assert main(["set"]) == 2 and main(["target"]) == 2


# --- golden-set drift --------------------------------------------------------
#
# A golden set is a fixed set of decisions replayed against the version you pinned. Finding drift
# there means the version served today is not the version served when the records were written,
# and every reliability curve measured against it describes a model that no longer exists.


from collections import Counter  # noqa: E402

from warrant.replay import AnswerDrift, ReplayResult, compare_answers  # noqa: E402


def _record_with(answers, model="typesafe/jev-1.13.0"):
    return {
        "record_id": "01K5A3Q7Z2XV8M9N4B6C1D0E01",
        "decision": {"class": "aml.alert.disposition", "subject": "alert:1", "action": "close", "answers": answers},
        "evidence": [{"name": "m", "type": "model_call", "uri": f"model://{model}", "content_hash": "a" * 64}],
    }


def test_identical_answers_are_no_drift():
    answers = [{"question": "disposition", "value": "close", "confidence": 0.94}]
    assert compare_answers(_record_with(answers), _record_with(list(answers))) == []


def test_a_changed_answer_value_is_drift():
    before = _record_with([{"question": "disposition", "value": "close", "confidence": 0.94}])
    after = _record_with([{"question": "disposition", "value": "escalate", "confidence": 0.94}])
    (drift,) = compare_answers(before, after)
    assert drift.value_changed and str(drift) == "disposition: 'close' -> 'escalate'"


def test_a_moved_confidence_is_drift_even_when_the_answer_is_the_same():
    """The quiet one: nothing flips, and every threshold tuned against the old numbers is wrong."""
    before = _record_with([{"question": "disposition", "value": "close", "confidence": 0.94}])
    after = _record_with([{"question": "disposition", "value": "close", "confidence": 0.97}])
    (drift,) = compare_answers(before, after)
    assert not drift.value_changed
    assert drift.confidence_delta == pytest.approx(0.03)
    assert "confidence 0.94 -> 0.97 (+0.030)" in str(drift)


def test_a_question_that_stopped_being_answered_is_drift():
    before = _record_with([
        {"question": "disposition", "value": "close", "confidence": 0.9},
        {"question": "structuring_pattern", "value": False, "confidence": 0.8},
    ])
    after = _record_with([{"question": "disposition", "value": "close", "confidence": 0.9}])
    (drift,) = compare_answers(before, after)
    assert drift.question == "structuring_pattern" and drift.after_value is None


def test_confidence_tolerance_separates_noise_from_signal():
    small = AnswerDrift("q", "close", "close", 0.940, 0.945)
    large = AnswerDrift("q", "close", "close", 0.940, 0.970)
    result = ReplayResult(
        record_id="r", subject="s", decision_class="c", status="replayed",
        before_action="close", after_action="close", before_mandate="allow", after_mandate="allow",
        before_cost=0.0, after_cost=0.0, outcome_label=None,
        drift=[small, large], confidence_tolerance=0.01,
    )
    assert result.confidence_drift == [large]  # 0.005 is below tolerance, 0.03 is above
    assert result.answer_drift == []


def test_replaying_against_the_same_model_reports_no_model_drift(recorded):
    """Ordinary replay against a target that asks for a different model is not drift."""
    ds = _set_from(recorded)
    target = Target("v2", AgentInfo("a", "2"), model="anthropic/claude-haiku-4-5-20251001")
    report = replay(ds, target, decide_v2)
    assert report.model_drifted == []  # the target asked for haiku and got haiku


def test_a_model_the_target_never_asked_for_is_drift(recorded):
    ds = _set_from(recorded)
    report = replay(ds, Target("v2", AgentInfo("a", "2")), decide_v2)  # no model named
    assert len(report.model_drifted) == 6
    assert report.model_transitions() == Counter(
        {"anthropic/claude-sonnet-5 -> anthropic/claude-haiku-4-5-20251001": 6}
    )
    assert "served by a different model" in report.summary()


def test_the_drift_gates_fail_a_run(recorded):
    ds = _set_from(recorded)
    report = replay(ds, Target("v2", AgentInfo("a", "2")), decide_v2, fail_on=["model-drift"])
    assert not report.passed
    assert report.failures() == ["6 decision(s) served by a different model"]

    clean = replay(ds, Target("v2", AgentInfo("a", "2"), model="anthropic/claude-haiku-4-5-20251001"),
                   decide_v2, fail_on=["model-drift", "answer-drift", "confidence-drift"])
    assert clean.passed


def test_an_unknown_gate_is_refused(recorded):
    with pytest.raises(ValueError) as exc:
        replay(_set_from(recorded), Target("v", AgentInfo("a", "1")), decide_v1, fail_on=["vibes"])
    assert "unknown fail-on gate" in str(exc.value)
