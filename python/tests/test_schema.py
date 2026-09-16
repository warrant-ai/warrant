import json
from pathlib import Path

import pytest

from warrant import SCHEMA_VERSION, ValidationError, __version__, load_schema, validate

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_version_is_set():
    assert __version__ == "0.0.1"
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
