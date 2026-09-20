"""The Jev adapter, exercised against a stubbed TypeSafe client.

No live call is made: it would need the user's API key and spend their credits, and the value here
is in the mapping and the guards, not in proving TypeSafe's servers answer. The response objects
are built from the real ``typesafe_sdk`` types where the package is installed, so a change to their
field names breaks this suite rather than production.
"""

import pytest

from warrant import AgentInfo, Warrant
from warrant.adapters.base import DecisionModel, ModelAnswer, ModelError, ModelResult
from warrant.adapters.jev import (
    JevAdapter,
    JevModel,
    LedgerUnavailable,
    ResidencyError,
    normalise_answer,
    region_of,
)

typesafe_sdk = pytest.importorskip("typesafe_sdk", reason="the Jev adapter needs the jev extra")


# --- stubs ------------------------------------------------------------------

#: Lets a fake distinguish "not given" from an explicitly empty distribution.
_DEFAULT = object()


class _StubClient:
    """Stands in for TypeSafeClient. Records the call and returns a prepared response."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def system_one(self, state, questions, *, model=None, **kwargs):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self._raises is not None:
            raise self._raises
        return self._response


def _response(answers, *, model="jev-1.13.0", tokens_in=1800, tokens_out=0):
    return typesafe_sdk.SystemOneResponse(
        model=model,
        usage=typesafe_sdk.Usage(input_tokens=tokens_in, output_tokens=tokens_out),
        answers=answers,
    )


def _choice(value="close", confidence=0.88, probabilities=_DEFAULT):
    # Jev's `confidence` is the margin between the top two probabilities, so it is deliberately
    # NOT equal to p(chosen) here. An earlier fake set the two to the same number, which is exactly
    # why the adapter recording the margin as a confidence went unnoticed until a live call.
    if probabilities is _DEFAULT:
        probabilities = {"close": 0.94, "escalate": 0.06}
    return typesafe_sdk.ChoiceAnswer(
        type="choice",
        choice=value,
        confidence=confidence,
        probabilities=probabilities,
    )


def _noul(p):
    return typesafe_sdk.NoulAnswer(type="noul", noul=p)


def _score(value=0.18, confidence=0.74, probabilities=None):
    return typesafe_sdk.ScoreAnswer(
        type="score", score=value, confidence=confidence,
        probabilities=probabilities or {0: 0.8, 1: 0.2}, legend={0: "low", 1: "high"},
    )


class _FakeModel(DecisionModel):
    """The acceptance criterion: interchangeable with the real adapter outside `adapters/`."""

    provider = "fake"
    endpoint = "https://fake.invalid"
    region = "global"

    def __init__(self, answers):
        self._answers = answers

    def evaluate(self, state, questions):
        return ModelResult(answers=self._answers, model="fake-1", tokens_in=10, tokens_out=0)


@pytest.fixture
def client(tmp_path):
    w = Warrant(
        "aml", tenant="demo-bank", store=tmp_path / "records.db",
        agent=AgentInfo("adjudicator", "1.0.0"), currency="INR", flush_interval=0.05,
    )
    yield w
    w.close()


def _decisions(client):
    assert client.flush(timeout=5)
    return [r for r in client.store.iter_records("aml") if r["record_type"] == "decision"]


# --- normalising the three primitives ---------------------------------------


def test_a_choice_keeps_its_distribution_not_only_the_winner():
    answer = normalise_answer("disposition", _choice())
    assert answer.value == "close"
    assert answer.distribution == {"close": 0.94, "escalate": 0.06}


def test_a_choice_states_the_probability_of_its_value_not_the_vendors_margin():
    # The record's `confidence` is read as "the stated probability that `value` is right" by every
    # reliability curve and every confidence floor in a policy. Jev's own field is the margin
    # between the top two probabilities (0.94 - 0.06), which is a smaller, different quantity.
    answer = normalise_answer("disposition", _choice(confidence=0.88))
    assert answer.confidence == 0.94


def test_a_choice_falls_back_to_the_vendors_number_only_when_there_is_no_distribution():
    answer = normalise_answer("disposition", _choice(confidence=0.71, probabilities={}))
    assert answer.confidence == 0.71


def test_a_score_keeps_the_rubric_distribution_and_states_no_confidence():
    # A score is the mean of the bucket distribution, so it is an expectation rather than a value
    # an outcome can equal. There is no probability that 0.18 is "right", so none is stated and
    # calibration leaves scores out rather than curving against a number that is not a probability.
    answer = normalise_answer("counterparty_risk", _score())
    assert answer.value == 0.18
    assert answer.confidence is None
    assert answer.distribution == {0: 0.8, 1: 0.2}


def test_a_score_carries_no_confidence_onto_the_record():
    assert "confidence" not in normalise_answer("counterparty_risk", _score()).as_record_answer()


@pytest.mark.parametrize(
    "p, expected_value, expected_confidence",
    [(0.93, True, 0.93), (0.07, False, 0.93), (0.5, True, 0.5), (0.49, False, 0.51)],
)
def test_a_noul_becomes_a_value_a_confidence_and_the_distribution_it_always_was(
    p, expected_value, expected_confidence
):
    # Jev gives a Noul no confidence field: it is one probability where 0.5 means "no idea".
    # Treating that as a two-outcome distribution is what makes yes/no questions calibratable.
    answer = normalise_answer("billing", _noul(p))
    assert answer.value is expected_value
    assert answer.confidence == pytest.approx(expected_confidence)
    assert answer.distribution[True] == pytest.approx(p)
    assert sum(answer.distribution.values()) == pytest.approx(1.0)


def test_an_unknown_primitive_is_refused_rather_than_guessed():
    class _Odd:
        type = "vector"

    with pytest.raises(ModelError) as exc:
        normalise_answer("q", _Odd())
    assert "unknown answer primitive" in str(exc.value)


# --- pinning ----------------------------------------------------------------


@pytest.mark.parametrize("alias", ["jev-latest", "jev-preview"])
def test_an_alias_is_refused_because_it_moves_under_a_calibration_curve(alias):
    with pytest.raises(ValueError) as exc:
        JevModel(model=alias, client=_StubClient())
    assert "alias that moves" in str(exc.value) and "allow_alias=True" in str(exc.value)


def test_an_alias_can_be_accepted_deliberately():
    model = JevModel(model="jev-latest", client=_StubClient(), allow_alias=True)
    assert model.pinned is False


def test_a_pinned_version_is_sent_on_every_call():
    stub = _StubClient(_response({"disposition": _choice()}))
    model = JevModel(model="jev-1.13.0", client=stub)
    model.evaluate({"a": 1}, {"disposition": object()})
    assert stub.calls[0]["model"] == "jev-1.13.0"
    assert model.pinned is True


def test_an_empty_model_string_is_refused():
    with pytest.raises(ValueError):
        JevModel(model="", client=_StubClient())


# --- residency --------------------------------------------------------------


def test_region_is_identified_or_reported_unknown_never_guessed():
    assert region_of("https://api.typesafe.ai") == "global"
    assert region_of("https://api.in.typesafe.ai") == "unknown"


def test_a_class_requiring_a_region_the_endpoint_cannot_serve_sends_nothing(client):
    stub = _StubClient(_response({"disposition": _choice()}))
    model = JevModel(model="jev-1.13.0", client=stub)
    adapter = JevAdapter(client, model, residency={"aml.alert.disposition": "in-india"})
    with pytest.raises(ResidencyError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"segment": "retail"}, questions={"disposition": object()},
        )
    assert "Nothing was sent" in str(exc.value)
    assert stub.calls == []
    assert _decisions(client) == []  # and it is not a decision, because nothing was decided


def test_a_class_without_a_residency_requirement_is_unaffected(client):
    stub = _StubClient(_response({"disposition": _choice()}))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
    )
    assert len(stub.calls) == 1


# --- one call decides and records -------------------------------------------


def test_one_call_produces_answers_a_route_and_a_sealed_record(client):
    stub = _StubClient(_response({"disposition": _choice(), "billing": _noul(0.91)}))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))

    result = adapter.decide(
        decision_class="aml.alert.disposition",
        subject="alert:TM-1",
        state={"segment": "retail", "amount_band": "1l_5l"},
        questions={"disposition": object(), "billing": object()},
        question_set=("aml.alert", "3.1.0"),
        cost_centre="fiu-ops",
    )

    assert result["disposition"].value == "close"
    assert result["disposition"].confidence == 0.94
    assert result.model == "jev-1.13.0"
    assert result.route in ("auto", "human")

    decision = _decisions(client)[0]["decision"]
    assert decision["question_set"] == {"id": "aml.alert", "version": "3.1.0"}
    assert decision["state_digest"] == result.state_digest
    answers = {a["question"]: a for a in decision["answers"]}
    assert answers["disposition"]["confidence"] == 0.94
    assert {d["value"]: d["p"] for d in answers["disposition"]["distribution"]} == {
        "close": 0.94, "escalate": 0.06
    }


def test_the_endpoint_and_the_persistence_mode_are_recorded(client):
    stub = _StubClient(_response({"disposition": _choice()}))
    adapter = JevAdapter(
        client, JevModel(model="jev-1.13.0", client=stub),
        persist={"aml.alert.disposition": "sync"},
        on_ledger_unavailable={"aml.alert.disposition": "closed"},
    )
    adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
    )
    evidence = {e["name"]: e["uri"] for e in _decisions(client)[0]["evidence"]}
    # where inference happened cannot be reconstructed later, so it is on the record
    assert "region=global" in evidence["model.endpoint"]
    assert "mode=sync" in evidence["warrant.persistence"]
    assert "on_unavailable=closed" in evidence["warrant.persistence"]


def test_the_state_digest_is_of_exactly_what_was_sent(client):
    from warrant.hashing import content_hash
    from warrant.adapters.jev import canonical_state

    stub = _StubClient(_response({"disposition": _choice()}))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    state = {"b": 2, "a": 1}
    result = adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state=state, questions={"disposition": object()},
    )
    assert result.state_digest == content_hash(canonical_state(state))
    # key order must not change the digest, or the same state hashes two ways
    assert result.state_digest == content_hash(canonical_state({"a": 1, "b": 2}))


def test_token_usage_becomes_a_cost_line(client):
    stub = _StubClient(_response({"disposition": _choice()}, tokens_in=4000, tokens_out=0))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
    )
    breakdown = _decisions(client)[0]["cost"]["breakdown"][0]
    assert breakdown["tokens_in"] == 4000 and breakdown["model"] == "jev-1.13.0"


# --- failure modes ----------------------------------------------------------


def test_a_vendor_failure_is_a_failed_decision_and_never_quotes_the_error_text(client):
    import httpx2

    vendor_error = typesafe_sdk.TypeSafeRateLimitError(
        status=429,
        body={"detail": "rate limited while evaluating state ABCDE1234F"},
        headers=httpx2.Headers({}),
        message="rate limited while evaluating state ABCDE1234F",
    )
    stub = _StubClient(raises=vendor_error)
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    with pytest.raises(ModelError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"pan": "ABCDE1234F"}, questions={"disposition": object()},
        )
    assert "TypeSafeRateLimitError" in str(exc.value)
    assert "ABCDE1234F" not in str(exc.value) and "rate limited" not in str(exc.value)

    record = _decisions(client)[0]
    assert record["decision"]["status"] == "failed"
    assert "ABCDE1234F" not in str(record)  # the vendor's message never reaches the ledger


def test_a_malformed_call_is_refused_before_a_scope_opens(client):
    stub = _StubClient(_response({"disposition": _choice()}))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    for kwargs in (
        {"subject": "", "state": {}, "questions": {"d": object()}},
        {"subject": "alert:1", "state": {}, "questions": {}},
    ):
        with pytest.raises(ValueError):
            adapter.decide(decision_class="aml.alert.disposition", **kwargs)
    # a malformed call is not a decision, so it must not leave a record
    assert _decisions(client) == []


def test_a_missing_disposition_answer_is_refused(client):
    stub = _StubClient(_response({"billing": _noul(0.9)}))
    adapter = JevAdapter(client, JevModel(model="jev-1.13.0", client=stub))
    with pytest.raises(ModelError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={}, questions={"billing": object()},
        )
    assert "did not answer" in str(exc.value)


def test_an_unknown_persistence_mode_is_refused_at_construction(client):
    model = JevModel(model="jev-1.13.0", client=_StubClient())
    with pytest.raises(ValueError):
        JevAdapter(client, model, persist={"x": "eventually"})
    with pytest.raises(ValueError):
        JevAdapter(client, model, on_ledger_unavailable={"x": "maybe"})


def test_fail_closed_raises_when_the_record_cannot_be_sealed(tmp_path):
    class _DeadClient(Warrant):
        def flush(self, timeout=5.0):
            return False

    w = _DeadClient(
        "aml", tenant="demo-bank", store=tmp_path / "r.db",
        agent=AgentInfo("adjudicator", "1.0.0"), flush_interval=0.05,
    )
    try:
        adapter = JevAdapter(
            w, JevModel(model="jev-1.13.0", client=_StubClient(_response({"disposition": _choice()}))),
            persist={"aml.alert.disposition": "sync"},
            on_ledger_unavailable={"aml.alert.disposition": "closed"},
            flush_timeout=0.1,
        )
        with pytest.raises(LedgerUnavailable) as exc:
            adapter.decide(
                decision_class="aml.alert.disposition", subject="alert:1",
                state={}, questions={"disposition": object()},
            )
        assert "must not be acted on" in str(exc.value)
    finally:
        w.close()


# --- the boundary holds -----------------------------------------------------


def test_a_fake_model_and_the_real_adapter_are_interchangeable(client):
    """The Implementation Brief's Stage 2 acceptance criterion, kept as a test.

    Swapping the decision model must need no change outside ``warrant.adapters``. This is the
    mitigation that makes depending on a days-old vendor survivable.
    """
    fake = _FakeModel({"disposition": ModelAnswer("disposition", "close", 0.88, {"close": 0.88, "escalate": 0.12})})
    adapter = JevAdapter(client, fake)
    result = adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
    )
    assert result["disposition"].value == "close" and result.model == "fake-1"
    decision = _decisions(client)[0]["decision"]
    assert decision["answers"][0]["confidence"] == 0.88


# --- the question set registry ----------------------------------------------


REGISTRY_DIR = __import__("pathlib").Path(__file__).resolve().parents[2] / "examples" / "gallery" / "aml" / "question-sets"


def _registry():
    pytest.importorskip("yaml")
    from warrant.questions import Registry

    return Registry.load(REGISTRY_DIR)


def _full_answers():
    """Every question in aml.alert@3.1.0, answered legally."""
    from warrant.adapters.base import ModelAnswer

    return {
        "profile_consistent": ModelAnswer("profile_consistent", True, 0.86),
        "structuring_pattern": ModelAnswer("structuring_pattern", False, 0.81),
        "counterparty_risk": ModelAnswer("counterparty_risk", 1.0, 0.74),
        "explanation_on_file": ModelAnswer("explanation_on_file", True, 0.90),
        "behaviour_change": ModelAnswer("behaviour_change", 0.0, 0.69),
        "disposition": ModelAnswer("disposition", "close", 0.94, {"close": 0.94, "escalate": 0.06}),
    }


def test_with_a_registry_an_unpinned_set_is_refused_before_anything_runs(client):
    fake = _FakeModel(_full_answers())
    adapter = JevAdapter(client, fake, registry=_registry())
    with pytest.raises(ValueError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"segment": "retail"}, questions={"disposition": object()},
        )
    assert "question_set=(id, version) is required" in str(exc.value)
    assert _decisions(client) == []


def test_a_version_the_registry_does_not_hold_is_refused(client):
    from warrant.questions import QuestionSetError

    adapter = JevAdapter(client, _FakeModel(_full_answers()), registry=_registry())
    with pytest.raises(QuestionSetError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"segment": "retail"}, questions={"disposition": object()},
            question_set=("aml.alert", "9.9.9"),
        )
    assert "not in the registry" in str(exc.value)
    assert _decisions(client) == []


def test_a_registered_set_records_normally(client):
    adapter = JevAdapter(client, _FakeModel(_full_answers()), registry=_registry())
    result = adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
        question_set=("aml.alert", "3.1.0"),
    )
    assert result.route in ("auto", "human")
    assert _decisions(client)[0]["decision"]["question_set"] == {"id": "aml.alert", "version": "3.1.0"}


def test_an_answer_outside_the_permitted_values_is_caught_where_it_happened(client):
    """Better here than as a distortion in a reliability curve three months later."""
    from warrant.adapters.base import ModelAnswer

    answers = _full_answers()
    answers["disposition"] = ModelAnswer("disposition", "shred", 0.99)
    adapter = JevAdapter(client, _FakeModel(answers), registry=_registry())
    with pytest.raises(ModelError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"segment": "retail"}, questions={"disposition": object()},
            question_set=("aml.alert", "3.1.0"),
        )
    assert "'shred' is not one of the permitted answers" in str(exc.value)
    # it failed inside the scope, so it is a failed decision rather than a silent one
    assert _decisions(client)[0]["decision"]["status"] == "failed"


def test_a_missing_answer_is_caught_too(client):
    answers = _full_answers()
    del answers["behaviour_change"]
    adapter = JevAdapter(client, _FakeModel(answers), registry=_registry())
    with pytest.raises(ModelError) as exc:
        adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:1",
            state={"segment": "retail"}, questions={"disposition": object()},
            question_set=("aml.alert", "3.1.0"),
        )
    assert "behaviour_change: not answered" in str(exc.value)


def test_without_a_registry_the_version_is_taken_on_trust(client):
    """Fine for a script; the registry is what a committee-facing deployment uses."""
    adapter = JevAdapter(client, _FakeModel(_full_answers()))
    adapter.decide(
        decision_class="aml.alert.disposition", subject="alert:1",
        state={"segment": "retail"}, questions={"disposition": object()},
        question_set=("made.up", "0.0.1"),
    )
    assert _decisions(client)[0]["decision"]["question_set"] == {"id": "made.up", "version": "0.0.1"}
