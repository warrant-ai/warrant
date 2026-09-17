import textwrap
from pathlib import Path

import pytest

pytest.importorskip("celpy")

from warrant import AgentInfo, Warrant  # noqa: E402
from warrant.cli import main  # noqa: E402
from warrant.policy import CelPolicyEngine, PolicyBundle, PolicyError, run_policy_tests  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_BUNDLE = ROOT / "examples" / "policies"


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


CR = """
policy_id: CR-07
version: "2026.3"
classes: [credit.approve]
fail_mode: {fail_mode}
default: escalate
clauses:
  - id: "4.1"
    title: Decline below bureau floor
    when: bureau_score < 650
    result: deny
  - id: "4.2"
    title: Auto-approve within limit
    when: amount <= 500000 && bureau_score >= 720 && double(foir) <= 0.45
    result: allow
"""


def test_example_bundle_loads_and_its_embedded_tests_pass():
    bundle = PolicyBundle.load(EXAMPLE_BUNDLE)
    assert sorted(p.policy_id for p in bundle.policies) == ["COL-02", "CR-07"]
    results = run_policy_tests(bundle)
    assert results and all(r.passed for r in results), [r.detail for r in results if not r.passed]


def test_first_matching_clause_wins_and_default_applies(tmp_path):
    _write(tmp_path, "cr.yaml", CR.format(fail_mode="closed"))
    engine = CelPolicyEngine(PolicyBundle.load(tmp_path))
    v = engine.evaluate("credit.approve", {"amount": 450000, "bureau_score": 748, "foir": 0.38})
    assert (v.result, v.policy_id, v.policy_version, v.clause, v.flagged) == ("allow", "CR-07", "2026.3", "4.2", False)
    assert v.allowed and v.reason == "Auto-approve within limit"
    v = engine.evaluate("credit.approve", {"amount": 10, "bureau_score": 600, "foir": 0.1})
    assert v.result == "deny" and v.clause == "4.1"
    v = engine.evaluate("credit.approve", {"amount": 10, "bureau_score": 700, "foir": 0.9})
    assert v.result == "escalate" and v.clause is None and v.reason == "no clause matched"


def test_unmapped_class_is_unchecked_and_wildcards_match_longest_prefix(tmp_path):
    _write(tmp_path, "a.yaml", CR.format(fail_mode="closed"))
    _write(tmp_path, "b.yaml", """
        policy_id: ALL
        version: "1"
        classes: [credit.*]
        clauses: [{id: "1", when: "true", result: deny}]
    """)
    _write(tmp_path, "c.yaml", """
        policy_id: CARDS
        version: "1"
        classes: [credit.card.*]
        clauses: [{id: "1", when: "true", result: escalate}]
    """)
    engine = CelPolicyEngine(PolicyBundle.load(tmp_path))
    assert engine.evaluate("credit.approve", {"amount": 1, "bureau_score": 800, "foir": 0.1}).policy_id == "CR-07"
    assert engine.evaluate("credit.limit_increase", {}).policy_id == "ALL"
    assert engine.evaluate("credit.card.issue", {}).policy_id == "CARDS"
    v = engine.evaluate("invoice.approve", {"amount": 1})
    assert v.result == "unchecked" and not v.allowed and "no policy governs" in v.reason


@pytest.mark.parametrize("fail_mode,expected", [("closed", "deny"), ("open", "allow"), ("escalate", "escalate")])
def test_fail_modes_apply_when_a_clause_cannot_evaluate(tmp_path, fail_mode, expected, caplog):
    _write(tmp_path, "cr.yaml", CR.format(fail_mode=fail_mode))
    engine = CelPolicyEngine(PolicyBundle.load(tmp_path))
    v = engine.evaluate("credit.approve", {"amount": 1})  # bureau_score missing
    assert v.result == expected and v.flagged and v.reason.startswith(f"fail-{fail_mode}: clause 4.1")
    assert v.allowed is (fail_mode == "open")
    assert any("fail-" + fail_mode in r.message for r in caplog.records)


def test_non_boolean_clause_and_bad_inputs_hit_fail_mode(tmp_path):
    _write(tmp_path, "p.yaml", """
        policy_id: P
        version: "1"
        classes: [x.y]
        fail_mode: open
        clauses: [{id: "1", when: "amount + 1", result: allow}]
    """)
    engine = CelPolicyEngine(PolicyBundle.load(tmp_path))
    v = engine.evaluate("x.y", {"amount": 1})
    assert v.flagged and "not bool" in v.reason
    v = engine.evaluate("x.y", {"amount": object()})
    assert v.flagged and "JSON" in v.reason


@pytest.mark.parametrize("body,match", [
    ("policy_id: ''\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'true', result: allow}]", "policy_id"),
    ("policy_id: P\nclasses: [a.b]\nclauses: [{id: '1', when: 'true', result: allow}]", "version"),
    ("policy_id: P\nversion: '1'\nclasses: [Bad Class]\nclauses: [{id: '1', when: 'true', result: allow}]", "invalid class"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nfail_mode: explode\nclauses: [{id: '1', when: 'true', result: allow}]", "fail_mode"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: []", "clauses"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'amount >', result: allow}]", "CEL parse error"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'true', result: maybe}]", "result must be"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'true', result: allow}, {id: '1', when: 'true', result: deny}]", "duplicate clause"),
    ("- not\n- a mapping", "top level"),
    ("policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'true', result: allow}]\ntests: [{inputs: {}, expect: perhaps}]", "expect must be"),
])
def test_malformed_policies_are_rejected_with_specific_errors(tmp_path, body, match):
    _write(tmp_path, "p.yaml", body)
    with pytest.raises(PolicyError, match=match):
        PolicyBundle.load(tmp_path)


def test_two_policies_claiming_one_class_is_an_error(tmp_path):
    for name in ("a", "b"):
        _write(tmp_path, f"{name}.yaml", f"policy_id: {name}\nversion: '1'\nclasses: [a.b]\nclauses: [{{id: '1', when: 'true', result: allow}}]")
    with pytest.raises(PolicyError, match="claimed by both"):
        PolicyBundle.load(tmp_path)


def test_missing_or_empty_bundle(tmp_path):
    with pytest.raises(FileNotFoundError):
        PolicyBundle.load(tmp_path / "nope")
    with pytest.raises(PolicyError, match="no policy files"):
        PolicyBundle.load(tmp_path)


def test_json_policy_file_is_accepted(tmp_path):
    _write(tmp_path, "p.json", '{"policy_id": "J", "version": "1", "classes": ["a.b"], "clauses": [{"id": "1", "when": "x > 1", "result": "allow"}]}')
    v = CelPolicyEngine(PolicyBundle.load(tmp_path)).evaluate("a.b", {"x": 2})
    assert v.result == "allow" and v.policy_id == "J"


def test_policy_tests_report_failures(tmp_path):
    _write(tmp_path, "p.yaml", """
        policy_id: P
        version: "1"
        classes: [a.b]
        clauses: [{id: "1", when: "x > 1", result: allow}]
        tests:
          - {name: passes, inputs: {x: 2}, expect: allow, clause: "1"}
          - {name: wrong result, inputs: {x: 0}, expect: allow}
          - {name: wrong clause, inputs: {x: 2}, expect: allow, clause: "9"}
    """)
    results = run_policy_tests(PolicyBundle.load(tmp_path))
    assert [(r.name, r.passed) for r in results] == [("passes", True), ("wrong result", False), ("wrong clause", False)]
    assert "expected allow, got deny" in results[1].detail
    assert "via clause 9" in results[2].detail


def test_warrant_with_policy_bundle_records_mandate(tmp_path):
    with Warrant("lending", store=tmp_path / "r.db", agent=AgentInfo("a", "1"), policy_bundle=EXAMPLE_BUNDLE, flush_interval=0.02) as w:
        with w.decide("credit.approve", subject="A") as d:
            assert d.check(amount=450000, bureau_score=748, foir=0.38).allowed
            d.act("approve")
        with w.decide("credit.approve", subject="B") as d:
            v = d.check(amount=450000)  # missing inputs, CR-07 is fail-closed
            assert not v.allowed and v.flagged
        with w.decide("invoice.approve", subject="C") as d:
            assert d.check(amount=1).result == "unchecked"
        assert w.flush()
        a, b, c = list(w.store.iter_records("lending"))
    assert a["mandate"] == {"result": "allow", "policy_id": "CR-07", "policy_version": "2026.3", "clause": "4.2", "reason": "Auto-approve up to 5,00,000 when bureau score >= 720 and FOIR <= 45%"}
    assert b["mandate"]["result"] == "deny" and b["mandate"]["flagged"] is True and b["mandate"]["reason"].startswith("fail-closed")
    assert b["decision"]["status"] == "withheld"
    assert c["mandate"] == {"result": "unchecked", "reason": "no policy governs class invoice.approve"}
    with pytest.raises(ValueError, match="not both"):
        Warrant("s", store=tmp_path / "x.db", agent=AgentInfo("a", "1"), policy=object(), policy_bundle=EXAMPLE_BUNDLE)


def test_cli_policy_test_and_check(tmp_path, capsys):
    assert main(["policy", "test", str(EXAMPLE_BUNDLE)]) == 0
    out = capsys.readouterr().out
    assert "7 passed, 0 failed across 2 policy file(s)" in out
    assert main(["policy", "check", str(EXAMPLE_BUNDLE), "credit.approve", "amount=450000", "bureau_score=748", "foir=0.38"]) == 0
    assert capsys.readouterr().out.startswith("allow  policy CR-07@2026.3  clause 4.2")
    assert main(["policy", "check", str(EXAMPLE_BUNDLE), "credit.approve", "amount=450000"]) == 0
    assert "FLAGGED" in capsys.readouterr().out
    assert main(["policy", "check", str(EXAMPLE_BUNDLE), "credit.approve", "amount"]) == 2
    assert "key=value" in capsys.readouterr().err
    _write(tmp_path, "bad.yaml", "policy_id: P\nversion: '1'\nclasses: [a.b]\nclauses: [{id: '1', when: 'x >', result: allow}]")
    assert main(["policy", "test", str(tmp_path)]) == 1
    assert "CEL parse error" in capsys.readouterr().err
    assert main(["policy", "test", str(tmp_path / "missing")]) == 1
    assert main(["policy"]) == 2


# -- portability ----------------------------------------------------------------


def _conformance():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent.parent.parent / "conformance" / "policy-cases.json").read_text(encoding="utf-8"))


def _evaluate_case(tmp_path, when, inputs):
    import json

    path = tmp_path / "bundle" / "p.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"policy_id": "T-1", "version": "1", "classes": ["t.run"], "fail_mode": "escalate", "default": "deny",
                                "clauses": [{"id": "x", "when": when, "result": "allow"}]}))
    verdict = CelPolicyEngine(PolicyBundle.load(path.parent)).evaluate("t.run", inputs)
    return "error" if verdict.flagged else verdict.result == "allow"


def test_shared_conformance_cases_evaluate_as_every_sdk_must(tmp_path):
    cases = _conformance()["cases"]
    assert len(cases) >= 30
    for case in cases:
        assert _evaluate_case(tmp_path, case["when"], case["inputs"]) == case["expect"], f"{case['name']}: {case['when']}"


def test_known_engine_differences_are_pinned_and_linted(tmp_path):
    from warrant.policy import lint_clause

    for case in _conformance()["divergent"]:
        assert _evaluate_case(tmp_path, case["when"], case["inputs"]) == case["python"], f"pinned Python behaviour changed: {case['name']}"
        assert lint_clause(case["when"]), case["when"]
        assert lint_clause(case["write_instead"]) == []


def test_lint_warns_at_load_and_stays_quiet_for_portable_clauses(tmp_path, caplog):
    import json
    from warrant.policy import lint_clause

    assert lint_clause("amount <= 500000 && double(foir) <= 0.45 && rate(x) > 0.5 && double(a) / double(b) < 1.0") == []
    assert len(lint_clause("0.45 >= applicant.foir")) == 1
    assert lint_clause("emi / income <= 0.45") == ["divides emi by income; whole numbers divide as integers, write double(emi) / double(income)"]
    (tmp_path / "p.json").write_text(json.dumps({"policy_id": "T-1", "version": "1", "classes": ["t.run"], "clauses": [{"id": "a", "when": "foir <= 0.45", "result": "allow"}]}))
    with caplog.at_level("WARNING", logger="warrant.policy"):
        PolicyBundle.load(tmp_path)
    assert caplog.messages == ["warrant policy T-1 clause a compares foir with a decimal literal; a whole-number input cannot be evaluated by every engine, write double(foir)"]
