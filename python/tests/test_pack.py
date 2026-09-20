import json
import subprocess
import sys

import pytest

from warrant import SQLiteStore, verify_records
from warrant.outcomes import ingest_outcomes
from warrant.pack import PackError, build_pack


def _decision(i, *, confidence=None, route="auto", policy=True):
    record = {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D{i:04d}",
        "record_type": "decision",
        "tenant": "demo-bank",
        "stream": "aml",
        "timestamp": f"2026-06-{(i % 28) + 1:02d}T10:00:00.000Z",
        "schema_version": "0",
        "origin": "live",
        "actor": {"name": "adjudicator", "version": "1.4.0"},
        "decision": {
            "class": "aml.alert.disposition",
            "action": "close",
            "subject": f"alert:TM-{i:04d}",
            "status": "acted",
            "route": route,
            "question_set": {"id": "aml.alert", "version": "3.1.0"},
        },
        "mandate": {"result": "allow"} if not policy else {"result": "allow", "policy_id": "AML-01", "policy_version": "2026.1"},
    }
    if confidence is not None:
        record["decision"]["answers"] = [
            {"question": "disposition", "value": "close", "confidence": confidence}
        ]
    return record


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "records.db")
    s.write([_decision(i, confidence=0.95) for i in range(20)])
    csv = tmp_path / "outcomes.csv"
    csv.write_text(
        "subject,label\n"
        + "".join(f"alert:TM-{i:04d},{'stayed_closed' if i % 4 else 'reopened'}\n" for i in range(16)),
        encoding="utf-8",
    )
    ingest_outcomes([csv], s, stream="aml")
    yield s
    s.close()


def test_pack_writes_records_manifest_coverage_and_a_front_page(tmp_path, store):
    result = build_pack(store, tmp_path / "pack", stream="aml", title="Q2 evidence")
    out = tmp_path / "pack"
    assert set(result.files) == {"records.jsonl", "manifest.json", "coverage.json", "README.md"}
    assert (out / "records.jsonl").exists()

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["records"] == 36 and manifest["by_type"] == {"decision": 20, "outcome": 16}
    assert manifest["verified"] is True
    assert manifest["policies"] == {"AML-01@2026.1": 20}
    assert manifest["question_sets"] == {"aml.alert@3.1.0": 20}
    assert manifest["chain_head_sequence"] == 36

    front = (out / "README.md").read_text()
    assert front.startswith("# Q2 evidence")
    assert "warrant verify records.jsonl" in front
    assert "16/20 (80.0%)" in front


def test_the_packed_records_verify_offline(tmp_path, store):
    build_pack(store, tmp_path / "pack", stream="aml")
    lines = (tmp_path / "pack" / "records.jsonl").read_text().splitlines()
    reports = verify_records([json.loads(line) for line in lines])
    assert [(r.stream, r.records, r.ok) for r in reports] == [("demo-bank/aml", 36, True)]


def test_the_front_page_states_what_the_chain_does_not_prove(tmp_path, store):
    build_pack(store, tmp_path / "pack", stream="aml")
    front = (tmp_path / "pack" / "README.md").read_text()
    # Claiming tamper-proof-against-the-operator would be false until per-writer signing ships.
    assert "does **not** show" in front
    assert "re-sealed the whole chain" in front
    assert "per-writer signing" in front


def test_calibration_section_appears_only_when_asked(tmp_path, store):
    plain = build_pack(store, tmp_path / "a", stream="aml")
    assert plain.calibration is None
    assert "## Calibration" not in (tmp_path / "a" / "README.md").read_text()

    scored = build_pack(
        store, tmp_path / "b", stream="aml", correct_when="outcome.label == 'stayed_closed'"
    )
    assert scored.calibration.usable == 16
    front = (tmp_path / "b" / "README.md").read_text()
    assert "Expected Calibration Error" in front
    assert json.loads((tmp_path / "b" / "calibration.json").read_text())["usable"] == 16


def test_where_narrows_the_curve_and_is_stated_on_the_page(tmp_path):
    s = SQLiteStore(tmp_path / "mixed.db")
    s.write([_decision(i, confidence=0.95, route="auto" if i < 5 else "human") for i in range(10)])
    csv = tmp_path / "o.csv"
    csv.write_text("subject,label\n" + "".join(f"alert:TM-{i:04d},stayed_closed\n" for i in range(10)))
    ingest_outcomes([csv], s, stream="aml")
    result = build_pack(
        s, tmp_path / "pack", stream="aml",
        correct_when="outcome.label == 'stayed_closed'", where="decision.route == 'auto'",
    )
    s.close()
    assert result.calibration.usable == 5
    assert "decision.route == 'auto'" in (tmp_path / "pack" / "README.md").read_text()


def test_policy_text_is_copied_in_when_a_bundle_is_given(tmp_path, store):
    bundle = tmp_path / "policies"
    bundle.mkdir()
    (bundle / "AML-01.yaml").write_text("policy_id: AML-01\nversion: '2026.1'\n", encoding="utf-8")
    result = build_pack(store, tmp_path / "pack", stream="aml", policy_dir=bundle)
    assert "policies/AML-01.yaml" in result.files
    assert (tmp_path / "pack" / "policies" / "AML-01.yaml").exists()
    assert "`policies/AML-01.yaml`" in (tmp_path / "pack" / "README.md").read_text()


# --- refusals ---------------------------------------------------------------


def test_a_non_empty_directory_is_refused_so_nothing_is_overwritten(tmp_path, store):
    out = tmp_path / "pack"
    out.mkdir()
    (out / "something.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(PackError) as exc:
        build_pack(store, out, stream="aml")
    assert "not empty" in str(exc.value)
    assert (out / "something.txt").read_text() == "mine"


def test_an_empty_stream_is_refused(tmp_path, store):
    with pytest.raises(PackError) as exc:
        build_pack(store, tmp_path / "pack", stream="nothing-here")
    assert "nothing to pack" in str(exc.value)
    assert not (tmp_path / "pack").exists()


def test_a_broken_chain_produces_no_pack_at_all(tmp_path, store):
    """An evidence pack that fails its own verification is worse than no pack."""

    class _Tampered:
        def __init__(self, records):
            self._records = records

        def iter_records(self, stream=None):
            return iter(self._records)

        def latest_outcome(self, decision_record_id):
            return None

    records = list(store.iter_records("aml"))
    records[3]["decision"]["action"] = "undo"  # breaks this record's hash and every link after it
    with pytest.raises(PackError) as exc:
        build_pack(_Tampered(records), tmp_path / "pack", stream="aml")
    assert "does not verify" in str(exc.value)
    assert not (tmp_path / "pack").exists()


def test_a_missing_policy_bundle_is_refused(tmp_path, store):
    with pytest.raises(PackError) as exc:
        build_pack(store, tmp_path / "pack", stream="aml", policy_dir=tmp_path / "absent")
    assert "no policy bundle" in str(exc.value)


def test_an_empty_policy_bundle_is_refused(tmp_path, store):
    bundle = tmp_path / "policies"
    bundle.mkdir()
    with pytest.raises(PackError) as exc:
        build_pack(store, tmp_path / "pack", stream="aml", policy_dir=bundle)
    assert "no policy files" in str(exc.value)


# --- CLI --------------------------------------------------------------------


def test_cli_pack_end_to_end(tmp_path):
    db = tmp_path / "cli.db"
    s = SQLiteStore(db)
    s.write([_decision(i, confidence=0.95) for i in range(10)])
    csv = tmp_path / "o.csv"
    csv.write_text("subject,label\n" + "".join(f"alert:TM-{i:04d},stayed_closed\n" for i in range(10)))
    ingest_outcomes([csv], s, stream="aml")
    s.close()

    out = tmp_path / "pack"
    result = subprocess.run(
        [sys.executable, "-m", "warrant.cli", "pack", "--store", str(db), "--stream", "aml",
         "-o", str(out), "--correct-when", "outcome.label == 'stayed_closed'"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "chain verified" in result.stdout

    # the pack is self-contained: verifying it needs nothing but the CLI and the directory
    verified = subprocess.run(
        [sys.executable, "-m", "warrant.cli", "verify", "records.jsonl"],
        cwd=out, capture_output=True, text=True,
    )
    assert verified.returncode == 0 and "chain OK" in verified.stdout
