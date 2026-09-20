import json
from pathlib import Path

import pytest

from warrant import SCHEMA_VERSION, ValidationError, __version__, load_schema, validate

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_version_matches_installed_metadata():
    from importlib.metadata import version

    assert __version__ == version("warrantai")
    assert SCHEMA_VERSION == "0"


def test_packaged_schema_matches_canonical_copy():
    canonical = ROOT / "schema" / "decision-record.v0.json"
    if not canonical.exists():
        pytest.skip("canonical schema only present in the source checkout")
    assert load_schema() == json.loads(canonical.read_text(encoding="utf-8"))


def test_loan_approval_example_is_valid():
    validate(_example("loan-approval.json"))


def test_loan_outcome_example_is_valid():
    validate(_example("loan-outcome.json"))


def test_decision_without_mandate_is_rejected():
    record = _example("loan-approval.json")
    del record["mandate"]
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("mandate" in m for m in exc.value.errors)


def test_unknown_mandate_result_is_rejected():
    record = _example("loan-approval.json")
    record["mandate"]["result"] = "maybe"
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any(m.startswith("mandate/result") for m in exc.value.errors)


def test_outcome_without_reference_is_rejected():
    record = _example("loan-outcome.json")
    del record["references"]
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("references" in m for m in exc.value.errors)


def test_unknown_top_level_field_is_rejected():
    record = _example("loan-approval.json")
    record["notes"] = "free text"
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("notes" in m for m in exc.value.errors)


def test_non_object_is_rejected():
    with pytest.raises(ValidationError):
        validate(["not", "a", "record"])


def _answered(**overrides) -> dict:
    """loan-approval with the optional question-set, state and answer fields filled in."""
    record = _example("loan-approval.json")
    record["decision"].update(
        {
            "route": "auto",
            "question_set": {"id": "credit.approve", "version": "3.1.0"},
            "state_digest": "a" * 64,
            "state_ref": "warrant://snapshot/01J8Z5M0000000000000000000",
            "answers": [
                {
                    "question": "disposition",
                    "value": "approve",
                    "confidence": 0.94,
                    "distribution": [
                        {"value": "approve", "p": 0.94},
                        {"value": "refer", "p": 0.05},
                        {"value": "decline", "p": 0.01},
                    ],
                },
                {"question": "affordability", "value": 0.72, "confidence": 0.81},
                {"question": "explanation_on_file", "value": True},
            ],
        }
    )
    record["decision"].update(overrides)
    return record


def test_decision_with_question_set_and_answers_is_valid():
    validate(_answered())


def test_records_without_the_new_fields_are_still_valid():
    # The fields are additive and optional: every record written before them keeps validating.
    record = _example("loan-approval.json")
    assert not set(record["decision"]) & {"route", "question_set", "state_digest", "answers"}
    validate(record)


def test_unknown_route_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate(_answered(route="autoclose"))
    assert any(m.startswith("decision/route") for m in exc.value.errors)


def test_non_semver_question_set_version_is_rejected():
    record = _answered()
    record["decision"]["question_set"]["version"] = "v3"
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("question_set/version" in m for m in exc.value.errors)


def test_question_set_without_a_version_is_rejected():
    record = _answered()
    del record["decision"]["question_set"]["version"]
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("question_set" in m for m in exc.value.errors)


def test_confidence_outside_zero_to_one_is_rejected():
    record = _answered()
    record["decision"]["answers"][0]["confidence"] = 1.4
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("answers/0/confidence" in m for m in exc.value.errors)


def test_answer_without_a_value_is_rejected():
    record = _answered()
    del record["decision"]["answers"][0]["value"]
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("answers/0" in m for m in exc.value.errors)


def test_unknown_field_inside_an_answer_is_rejected():
    record = _answered()
    record["decision"]["answers"][0]["rationale"] = "free text that belongs in an excerpt"
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("rationale" in m for m in exc.value.errors)


def test_distribution_entry_without_a_probability_is_rejected():
    record = _answered()
    del record["decision"]["answers"][0]["distribution"][1]["p"]
    with pytest.raises(ValidationError) as exc:
        validate(record)
    assert any("distribution/1" in m for m in exc.value.errors)


def test_state_digest_must_be_a_sha256():
    with pytest.raises(ValidationError) as exc:
        validate(_answered(state_digest="not-a-hash"))
    assert any("state_digest" in m for m in exc.value.errors)
