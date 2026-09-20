import json
import random
import subprocess
import sys

import pytest

from warrant import SQLiteStore
from warrant.calibrate import CalibrationError, calibrate, confidence_of, gate

celpy = pytest.importorskip("celpy", reason="calibration predicates need the policy extra")


def _decision(i, *, confidence=None, answers=None, label=None, cls="aml.alert.disposition", question_set="3.1.0", route="auto"):
    record = {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D{i:04d}",
        "record_type": "decision",
        "tenant": "demo-bank",
        "stream": "aml",
        "timestamp": "2026-06-01T10:00:00.000Z",
        "schema_version": "0",
        "origin": "live",
        "actor": {"name": "adjudicator", "version": "1.0.0"},
        "decision": {
            "class": cls,
            "action": "close",
            "subject": f"alert:A-{i:04d}",
            "status": "acted",
            "route": route,
            "question_set": {"id": "aml.alert", "version": question_set},
        },
        "mandate": {"result": "allow"},
    }
    if answers is not None:
        record["decision"]["answers"] = answers
    elif confidence is not None:
        record["decision"]["answers"] = [{"question": "disposition", "value": "close", "confidence": confidence}]
    return record, label


def _outcome(i, decision_record_id, label):
    return {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1E{i:04d}",
        "record_type": "outcome",
        "tenant": "demo-bank",
        "stream": "aml",
        "timestamp": "2026-09-01T10:00:00.000Z",
        "schema_version": "0",
        "origin": "live",
        "references": {"decision_record_id": decision_record_id},
        "outcome": {"status": "observed", "label": label, "observed_at": "2026-09-01T10:00:00.000Z"},
    }


def _store(tmp_path, pairs, name="records.db"):
    """``pairs`` is a list of (decision, outcome_label|None)."""
    store = SQLiteStore(tmp_path / name)
    records = []
    for i, (decision, label) in enumerate(pairs):
        records.append(decision)
        if label is not None:
            records.append(_outcome(i, decision["record_id"], label))
    store.write(records)
    return store


RIGHT = "outcome.label == 'stayed_closed'"


# --- the measurement --------------------------------------------------------


def test_a_perfectly_calibrated_decider_has_near_zero_ece(tmp_path):
    random.seed(11)
    pairs = []
    for i in range(2000):
        stated = random.choice([0.55, 0.65, 0.75, 0.85, 0.95])
        right = random.random() < stated  # observed frequency equals the stated probability
        pairs.append(_decision(i, confidence=stated, label="stayed_closed" if right else "reopened"))
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert report.usable == 2000
    assert report.ece < 0.03
    assert report.accuracy == pytest.approx(report.mean_confidence, abs=0.03)


def test_a_systematically_overconfident_decider_is_caught_and_quantified(tmp_path):
    random.seed(11)
    gap = 0.15
    pairs = []
    for i in range(2000):
        stated = random.choice([0.55, 0.65, 0.75, 0.85, 0.95])
        right = random.random() < stated - gap
        pairs.append(_decision(i, confidence=stated, label="stayed_closed" if right else "reopened"))
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert report.ece == pytest.approx(gap, abs=0.03)
    assert all(b.gap < 0 for b in report.buckets if b.count)  # every band overstated itself
    assert report.mce >= report.ece


def test_decisions_without_an_outcome_are_excluded_and_counted(tmp_path):
    pairs = [
        _decision(0, confidence=0.9, label="stayed_closed"),
        _decision(1, confidence=0.9, label=None),
        _decision(2, confidence=0.9, label=None),
    ]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert (report.decisions, report.with_confidence, report.with_outcome, report.usable) == (3, 3, 1, 1)
    assert report.to_dict()["outcome_attached_share"] == pytest.approx(1 / 3)
    assert "33.3% attached" in report.summary()


def test_decisions_without_a_confidence_are_excluded(tmp_path):
    pairs = [_decision(0, confidence=0.9, label="stayed_closed"), _decision(1, label="stayed_closed")]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert report.decisions == 2 and report.with_confidence == 1 and report.usable == 1


def test_nothing_measurable_reports_honestly_rather_than_returning_zero(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label=None)])
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert report.usable == 0 and report.ece == 0.0
    assert "nothing to measure" in report.summary()


def test_brier_rewards_sharpness_not_only_calibration(tmp_path):
    hedged = _store(tmp_path, [_decision(i, confidence=0.5, label="stayed_closed" if i % 2 else "reopened") for i in range(100)], "a.db")
    sharp = _store(tmp_path, [_decision(i, confidence=0.99 if i % 2 else 0.01, label="stayed_closed" if i % 2 else "reopened") for i in range(100)], "b.db")
    hedged_report = calibrate(hedged, correct_when=RIGHT, stream="aml")
    sharp_report = calibrate(sharp, correct_when=RIGHT, stream="aml")
    hedged.close()
    sharp.close()
    # both are perfectly calibrated; only Brier distinguishes the useless one
    assert hedged_report.ece < 0.02 and sharp_report.ece < 0.02
    assert sharp_report.brier < hedged_report.brier


# --- what counts as right ---------------------------------------------------


def test_correct_when_is_required(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label="stayed_closed")])
    with pytest.raises(CalibrationError) as exc:
        calibrate(store, correct_when="", stream="aml")
    store.close()
    assert "correct-when" in str(exc.value)


def test_an_invalid_predicate_is_rejected_before_any_output(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label="stayed_closed")])
    with pytest.raises(CalibrationError) as exc:
        calibrate(store, correct_when="outcome.label ===== 'x'", stream="aml")
    store.close()
    assert "invalid --correct-when" in str(exc.value)


def test_a_predicate_naming_an_absent_field_scores_false_not_an_error(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label="stayed_closed")])
    report = calibrate(store, correct_when="outcome.nosuchfield == 1", stream="aml")
    store.close()
    assert report.usable == 1 and report.accuracy == 0.0


def test_the_predicate_can_read_decision_fields_too(tmp_path):
    pairs = [
        _decision(0, confidence=0.9, label="stayed_closed", route="auto"),
        _decision(1, confidence=0.9, label="stayed_closed", route="human"),
    ]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when="outcome.label == 'stayed_closed' && decision.route == 'auto'", stream="aml")
    store.close()
    assert report.usable == 2 and report.accuracy == 0.5


# --- picking the confidence -------------------------------------------------


def test_several_scored_answers_without_answer_is_an_error_naming_the_flag(tmp_path):
    answers = [
        {"question": "disposition", "value": "close", "confidence": 0.9},
        {"question": "structuring", "value": "no", "confidence": 0.7},
    ]
    store = _store(tmp_path, [_decision(0, answers=answers, label="stayed_closed")])
    with pytest.raises(CalibrationError) as exc:
        calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert "--answer" in str(exc.value) and "disposition" in str(exc.value)


def test_answer_selects_the_named_question(tmp_path):
    answers = [
        {"question": "disposition", "value": "close", "confidence": 0.9},
        {"question": "structuring", "value": "no", "confidence": 0.2},
    ]
    store = _store(tmp_path, [_decision(0, answers=answers, label="stayed_closed")])
    report = calibrate(store, correct_when=RIGHT, stream="aml", answer="structuring")
    store.close()
    assert report.usable == 1 and report.mean_confidence == pytest.approx(0.2)


def test_an_unscored_answer_is_not_a_confidence():
    record, _ = _decision(0, answers=[{"question": "disposition", "value": "close"}])
    assert confidence_of(record, None) is None


def test_a_boolean_is_not_mistaken_for_a_confidence():
    record, _ = _decision(0, answers=[{"question": "flag", "value": True, "confidence": 0.5}])
    assert confidence_of(record, None) == 0.5
    plain, _ = _decision(1, answers=[{"question": "flag", "value": True}])
    assert confidence_of(plain, None) is None


class _StubStore:
    """A store that yields whatever it is given, so records the schema would reject can be tested."""

    def __init__(self, records):
        self._records = records

    def iter_records(self, stream=None):
        return iter(self._records)

    def latest_outcome(self, decision_record_id):
        return {"outcome": {"status": "observed", "label": "stayed_closed"}}


def test_a_confidence_outside_zero_to_one_is_refused(tmp_path):
    # The schema caps confidence at 1, so this can only arrive from a writer that skipped validation
    # or from older data. Bucketing it silently would corrupt the curve, so calibrate refuses.
    record, _ = _decision(0, answers=[{"question": "disposition", "value": "close", "confidence": 1.4}])
    with pytest.raises(CalibrationError) as exc:
        calibrate(_StubStore([record]), correct_when=RIGHT, stream="aml")
    assert "outside 0..1" in str(exc.value)


def test_confidence_at_the_boundaries_lands_in_the_end_buckets(tmp_path):
    pairs = [
        _decision(0, confidence=1.0, label="stayed_closed"),
        _decision(1, confidence=0.0, label="reopened"),
    ]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert report.buckets[-1].count == 1 and report.buckets[0].count == 1
    assert report.usable == 2 and report.ece == pytest.approx(0.0)


# --- shape ------------------------------------------------------------------


def test_buckets_must_be_at_least_two(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label="stayed_closed")])
    with pytest.raises(CalibrationError):
        calibrate(store, correct_when=RIGHT, stream="aml", buckets=1)
    store.close()


def test_breakdown_by_question_set_and_class(tmp_path):
    pairs = [
        _decision(0, confidence=0.9, label="stayed_closed", question_set="3.1.0"),
        _decision(1, confidence=0.9, label="reopened", question_set="3.1.0"),
        _decision(2, confidence=0.9, label="stayed_closed", question_set="4.0.0"),
    ]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml", by="question_set")
    store.close()
    assert set(report.breakdown) == {"aml.alert@3.1.0", "aml.alert@4.0.0"}
    assert report.breakdown["aml.alert@3.1.0"].accuracy == 0.5
    assert report.breakdown["aml.alert@4.0.0"].accuracy == 1.0
    assert "by question_set:" in report.summary()


def test_breakdown_by_an_inputs_field(tmp_path):
    pairs = []
    for i, segment in enumerate(["retail", "retail", "trade_finance"]):
        decision, label = _decision(i, confidence=0.9, label="stayed_closed")
        decision["decision"]["inputs"] = {"segment": segment}
        pairs.append((decision, label))
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml", by="inputs.segment")
    store.close()
    assert report.breakdown["retail"].usable == 2
    assert report.breakdown["trade_finance"].usable == 1


def test_an_unknown_breakdown_dimension_is_refused(tmp_path):
    store = _store(tmp_path, [_decision(0, confidence=0.9, label="stayed_closed")])
    with pytest.raises(CalibrationError) as exc:
        calibrate(store, correct_when=RIGHT, stream="aml", by="colour")
    store.close()
    assert "unknown --by dimension" in str(exc.value)


def test_gate_reports_threshold_breaches(tmp_path):
    random.seed(3)
    pairs = [
        _decision(i, confidence=0.95, label="stayed_closed" if i % 2 else "reopened")
        for i in range(100)
    ]
    store = _store(tmp_path, pairs)
    report = calibrate(store, correct_when=RIGHT, stream="aml")
    store.close()
    assert gate(report) == []
    failures = gate(report, max_ece=0.1, max_mce=0.1)
    assert len(failures) == 2 and "ECE" in failures[0] and "MCE" in failures[1]


# --- CLI --------------------------------------------------------------------


def test_cli_calibrate_prints_a_curve_and_gates(tmp_path):
    random.seed(5)
    pairs = [
        _decision(i, confidence=0.9, label="stayed_closed" if random.random() < 0.6 else "reopened")
        for i in range(200)
    ]
    store = _store(tmp_path, pairs, "cli.db")
    store.close()
    out = tmp_path / "calibration.json"

    result = subprocess.run(
        [sys.executable, "-m", "warrant.cli", "calibrate", "--store", str(tmp_path / "cli.db"),
         "--stream", "aml", "--correct-when", RIGHT, "--report", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ECE" in result.stdout
    payload = json.loads(out.read_text())
    assert payload["usable"] == 200 and payload["correct_when"] == RIGHT

    gated = subprocess.run(
        [sys.executable, "-m", "warrant.cli", "calibrate", "--store", str(tmp_path / "cli.db"),
         "--stream", "aml", "--correct-when", RIGHT, "--max-ece", "0.01"],
        capture_output=True, text=True,
    )
    assert gated.returncode == 1 and "FAILED" in gated.stderr
