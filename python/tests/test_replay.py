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
    assert data["totals"] == {"replayed": 6, "flipped": 2, "new_deny": 0, "new_escalate": 0, "unreplayable": 0, "errored": 0, "cost_before": 12.0, "cost_after": 3.0, "cost_change_pct": -75.0}
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
