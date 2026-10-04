"""The Python and JavaScript SDKs seal and verify each other's records (scripts/differential.py)."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node on PATH")

_spec = importlib.util.spec_from_file_location("differential", SCRIPTS / "differential.py")
differential = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(differential)


def test_both_sdks_agree_on_random_values_and_records():
    assert differential.run(seed=7, count=400) == 400


def test_the_node_side_reports_a_value_python_canonicalised_differently(tmp_path):
    key = differential.SigningKey("differential-python", bytes(range(32)))
    case = next(differential._cases(7, 1, key))
    case["value"], case["canonical"] = {"amount": 4.0}, '{"amount":4.0}'  # the pre-0.9.0 Python form
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"key": key.public.to_dict()}) + "\n" + json.dumps(case) + "\n", encoding="utf-8")
    done = subprocess.run(["node", str(SCRIPTS / "differential.mjs"), str(cases), str(tmp_path / "out.jsonl")], capture_output=True, text=True)
    assert done.returncode == 1
    assert "canonical JSON differs" in done.stderr and "content hash differs" in done.stderr


def test_an_altered_record_fails_in_the_other_language(tmp_path, monkeypatch):
    real = differential.seal_record

    def tampering(record, seq, prev, signer):
        sealed = real(record, seq, prev, signer)
        if seq == 3:
            sealed["payload"] = "altered after sealing"
        return sealed

    monkeypatch.setattr(differential, "seal_record", tampering)
    with pytest.raises(differential.DifferentialFailure, match="record hash differs"):
        differential.run(seed=7, count=5)


def test_a_missing_node_is_a_failure_not_a_pass():
    with pytest.raises(differential.DifferentialFailure, match="could not run"):
        differential.run(seed=7, count=1, node="no-such-node-binary")
    with pytest.raises(ValueError, match="at least 1"):
        differential.run(seed=7, count=0)
