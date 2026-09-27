"""The Laya adapter, exercised against a fake Laya agent, plus one opt-in live check.

The fake's ``confidence`` is normalised entropy, deliberately different from p(chosen), because
that is what real Laya returns. A fake that made the two equal would hide exactly the mistake the
Jev adapter shipped with until a live call.
"""

import math
import os

import pytest

from warrant import AgentInfo, SQLiteStore, Warrant
from warrant.adapters.base import ModelError
from warrant.adapters.laya import (
    ENDPOINT,
    REGION,
    LayaAdapter,
    LayaModel,
    normalise_answer,
    to_laya_question,
)
from warrant.adapters.model import ResidencyError
from warrant.questions import Question

SHA = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"


def _entropy_confidence(probs):
    k = len(probs)
    h = -sum(p * math.log(max(p, 1e-12)) for p in probs)
    return 1.0 - h / math.log(k)


def _choice(probs):
    chosen = max(probs, key=probs.get)
    return {
        "type": "choice",
        "choice": chosen,
        "probabilities": probs,
        "confidence": round(_entropy_confidence(list(probs.values())), 4),
        "answer_confidence": max(probs.values()),
    }


class _FakeAgent:
    """Stands in for a loaded Laya agent. Records the call and returns a prepared response."""

    def __init__(self, answers=None, raises=None):
        self.answers = answers
        self.raises = raises
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self.raises is not None:
            raise self.raises
        return {"answers": self.answers, "model": "laya-rl-agent", "usage": {"input_tokens": 341, "output_tokens": 0}}


def _answers():
    return {
        "disposition": _choice({"escalate": 0.6452, "close": 0.3548}),
        "structuring": {"type": "noul", "noul": 0.2, "confidence": 0.8, "answer_confidence": 0.8},
        "risk": {"type": "score", "score": 1.84, "probabilities": {"0": 0.13, "1": 0.18, "2": 0.42, "3": 0.27},
                 "confidence": 0.09, "answer_confidence": 0.42, "legend": {}},
    }


QUESTIONS = {
    "disposition": Question("disposition", "choice", "Escalate for STR review or close?", ("escalate", "close")),
    "structuring": {"primitive": "noul", "instructions": "Deposits split under a threshold?"},
    "risk": {"type": "score", "instructions": "Rate laundering risk.", "criteria": ["none", "low", "medium", "high"]},
}


# --- pinning and identity -----------------------------------------------------


@pytest.mark.parametrize("revision", [None, "main", "55cf4c4e", SHA.upper()])
def test_an_unpinned_revision_is_refused(revision):
    with pytest.raises(ValueError, match="commit sha"):
        LayaModel(revision=revision, agent=_FakeAgent())


def test_unpinned_is_allowed_only_when_accepted_and_is_visible_in_the_identifier():
    m = LayaModel(agent=_FakeAgent(), allow_unpinned=True)
    assert not m.pinned
    assert m.model == "laya@unpinned/english"


def test_the_identifier_names_the_revision_and_checkpoint_not_lay_constant():
    fake = _FakeAgent(_answers())
    m = LayaModel(revision=SHA, subfolder="multilingual", agent=fake)
    result = m.evaluate({"alert": "x"}, QUESTIONS)
    assert result.model == "laya@55cf4c4ebb4e/multilingual"
    assert result.model != "laya-rl-agent"
    assert LayaModel(revision=SHA, agent=fake).model == "laya@55cf4c4ebb4e/english"


# --- mapping ------------------------------------------------------------------


def test_a_choice_records_p_chosen_never_laya_confidence():
    a = normalise_answer("d", _choice({"escalate": 0.6452, "close": 0.3548}))
    assert a.value == "escalate"
    assert a.confidence == pytest.approx(0.6452)
    # Laya's own number is entropy on a different scale; if the adapter copied it this would be ~0.06.
    assert a.confidence != pytest.approx(_choice({"escalate": 0.6452, "close": 0.3548})["confidence"])
    assert a.distribution == {"escalate": 0.6452, "close": 0.3548}


def test_a_choice_without_a_distribution_falls_back_to_answer_confidence():
    a = normalise_answer("d", {"type": "choice", "choice": "close", "probabilities": {}, "confidence": 0.02, "answer_confidence": 0.58})
    assert a.confidence == pytest.approx(0.58)
    assert a.distribution is None


@pytest.mark.parametrize("p,value,conf", [(0.2, False, 0.8), (0.91, True, 0.91), (0.5, True, 0.5)])
def test_a_noul_is_a_two_outcome_distribution(p, value, conf):
    a = normalise_answer("s", {"type": "noul", "noul": p, "confidence": 0.123})
    assert a.value is value
    assert a.confidence == pytest.approx(conf)
    assert a.distribution == {True: p, False: pytest.approx(1 - p)}


def test_a_score_states_no_confidence_and_keeps_integer_buckets():
    a = normalise_answer("r", _answers()["risk"])
    assert a.value == pytest.approx(1.84)
    assert a.confidence is None
    assert a.distribution == {0: 0.13, 1: 0.18, 2: 0.42, 3: 0.27}


def test_an_unknown_primitive_is_a_model_error():
    with pytest.raises(ModelError):
        normalise_answer("x", {"type": "ranking"})


# --- question translation ------------------------------------------------------


def test_questions_translate_from_warrant_and_laya_forms():
    assert to_laya_question("d", QUESTIONS["disposition"]) == {
        "type": "choice", "instructions": "Escalate for STR review or close?", "criteria": {"escalate": None, "close": None}}
    assert to_laya_question("s", QUESTIONS["structuring"]) == {"type": "noul", "instructions": "Deposits split under a threshold?"}
    assert to_laya_question("r", QUESTIONS["risk"])["criteria"] == ["none", "low", "medium", "high"]


@pytest.mark.parametrize("spec", [
    object(),
    {"type": "ranking", "instructions": "x"},
    {"type": "choice", "instructions": "x"},
    {"type": "score", "instructions": "x", "criteria": []},
    {"type": "noul", "instructions": ""},
    {"type": "noul", "instructions": "x", "criteria": {"yes": "y"}},
])
def test_a_malformed_question_is_refused(spec):
    with pytest.raises(ValueError):
        to_laya_question("q", spec)


def test_a_malformed_question_never_reaches_the_model():
    fake = _FakeAgent(_answers())
    with pytest.raises(ModelError):
        LayaModel(revision=SHA, agent=fake).evaluate("s", {"q": {"type": "choice", "instructions": "x"}})
    assert fake.calls == []


# --- end to end through DecisionAdapter ---------------------------------------


def _warrant(tmp_path):
    return Warrant("aml", tenant="demo-bank", store=tmp_path / "r.db", agent=AgentInfo("adjudicator", "1.0.0"), flush_interval=0.02)


def _decisions(tmp_path):
    return [r for r in SQLiteStore(tmp_path / "r.db", read_only=True).iter_records("aml") if r["record_type"] == "decision"]


def test_in_process_inference_satisfies_any_residency_requirement_and_is_recorded(tmp_path):
    w = _warrant(tmp_path)
    try:
        adapter = LayaAdapter(w, LayaModel(revision=SHA, subfolder="multilingual", agent=_FakeAgent(_answers())),
                              residency={"aml.alert.disposition": "IN"})
        result = adapter.decide(decision_class="aml.alert.disposition", subject="alert:1",
                                state={"alert": "9 deposits of 49,000"}, questions=QUESTIONS)
        assert w.flush(timeout=5)
    finally:
        w.close()
    assert result.model == "laya@55cf4c4ebb4e/multilingual"
    record = _decisions(tmp_path)[0]
    endpoint = next(e for e in record["evidence"] if e["name"] == "model.endpoint")
    assert endpoint["uri"] == f"{ENDPOINT}?region={REGION}"
    answers = {a["question"]: a for a in record["decision"]["answers"]}
    assert answers["disposition"]["confidence"] == pytest.approx(0.6452)
    assert "confidence" not in answers["risk"]
    model_cost = [c for c in record["cost"]["breakdown"] if c["kind"] == "model_call"][0]
    assert model_cost["provider"] == "convai-laya" and model_cost["model"] == "laya@55cf4c4ebb4e/multilingual"
    assert model_cost["tokens_in"] == 341


def test_a_remote_model_is_still_refused_where_residency_requires_a_region(tmp_path):
    class _Remote(LayaModel):
        @property
        def region(self):
            return "global"

    w = _warrant(tmp_path)
    try:
        adapter = LayaAdapter(w, _Remote(revision=SHA, agent=_FakeAgent(_answers())), residency={"aml.alert.disposition": "IN"})
        with pytest.raises(ResidencyError):
            adapter.decide(decision_class="aml.alert.disposition", subject="alert:2", state="s", questions=QUESTIONS)
    finally:
        w.close()


def test_the_error_text_never_reaches_the_record_or_the_exception(tmp_path):
    secret = "PAN ABCDE1234F"
    w = _warrant(tmp_path)
    try:
        adapter = LayaAdapter(w, LayaModel(revision=SHA, agent=_FakeAgent(raises=RuntimeError(f"cannot read {secret}"))))
        with pytest.raises(ModelError) as info:
            adapter.decide(decision_class="aml.alert.disposition", subject="alert:3", state="s", questions=QUESTIONS)
        assert w.flush(timeout=5)
    finally:
        w.close()
    assert secret not in str(info.value)
    record = _decisions(tmp_path)[0]
    assert record["decision"]["status"] == "failed"
    assert secret not in str(record)


# --- live, opt-in -------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("WARRANT_LAYA_LIVE") != "1", reason="set WARRANT_LAYA_LIVE=1 to load real Laya weights")
def test_live_multilingual_checkpoint_maps_confidence_to_p_chosen():
    pytest.importorskip("laya")
    m = LayaModel(revision=SHA, subfolder="multilingual")
    result = m.evaluate(
        {"customer": "salaried clerk, income 4.2 lakh", "alert": "9 cash deposits of 49,000 across 4 branches, then RTGS 4.3 lakh"},
        QUESTIONS,
    )
    raw = m._agent.system_one(
        {"customer": "salaried clerk, income 4.2 lakh", "alert": "9 cash deposits of 49,000 across 4 branches, then RTGS 4.3 lakh"},
        {k: to_laya_question(k, v) for k, v in QUESTIONS.items()},
    )["answers"]
    d = result["disposition"]
    assert d.confidence == pytest.approx(raw["disposition"]["probabilities"][d.value], abs=1e-4)
    assert d.confidence != pytest.approx(raw["disposition"]["confidence"], abs=1e-3)
    assert result["risk"].confidence is None
    assert result.model == "laya@55cf4c4ebb4e/multilingual"
