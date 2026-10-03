import json
import subprocess
import sys

import pytest

from warrant import SQLiteStore, verify_records
from warrant.outcomes import (
    OutcomeFileError,
    build_outcome_record,
    coverage,
    ingest_outcomes,
    iter_joined,
    read_outcome_file,
)


def _decision(i, *, stream="aml", confidence=None, origin="live", cls="aml.alert.disposition"):
    record = {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D0E{i:02d}",
        "record_type": "decision",
        "tenant": "demo-bank",
        "stream": stream,
        "timestamp": "2026-06-01T10:00:00.000Z",
        "schema_version": "0",
        "origin": origin,
        "actor": {"name": "adjudicator", "version": "1.0.0"},
        "decision": {"class": cls, "action": "close", "subject": f"alert:A-{i:02d}", "status": "acted"},
        "mandate": {"result": "allow"},
    }
    if confidence is not None:
        record["decision"]["answers"] = [
            {"question": "disposition", "value": "close", "confidence": confidence}
        ]
    return record


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "records.db")
    s.write([_decision(i) for i in range(4)])
    yield s
    s.close()


def _csv(tmp_path, text, name="outcomes.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- reading the file -------------------------------------------------------


def test_reads_subject_label_and_optional_columns(tmp_path):
    path = _csv(
        tmp_path,
        "subject,label,observed_at,score,source\n"
        "alert:A-00,stayed_closed,2026-09-01T00:00:00Z,0.12,case-system\n",
    )
    rows, invalid = read_outcome_file(path)
    assert invalid == []
    assert rows[0].subject == "alert:A-00"
    assert rows[0].label == "stayed_closed"
    assert rows[0].score == 0.12
    assert rows[0].source == "case-system"


def test_column_aliases_and_bom_are_accepted(tmp_path):
    path = _csv(tmp_path, "﻿subject_ref,label\nalert:A-00,reopened\n")
    rows, invalid = read_outcome_file(path)
    assert invalid == [] and rows[0].subject == "alert:A-00"


def test_naive_observed_at_is_normalised_to_utc(tmp_path):
    path = _csv(tmp_path, "subject,label,observed_at\nalert:A-00,x,2026-09-01T05:30:00\n")
    rows, _ = read_outcome_file(path)
    assert rows[0].observed_at == "2026-09-01T05:30:00.000Z"


def test_file_without_a_label_column_is_refused(tmp_path):
    path = _csv(tmp_path, "subject,result\nalert:A-00,closed\n")
    with pytest.raises(OutcomeFileError) as exc:
        read_outcome_file(path)
    assert "label" in str(exc.value)


def test_file_without_subject_or_record_id_is_refused(tmp_path):
    path = _csv(tmp_path, "label,observed_at\nclosed,2026-09-01T00:00:00Z\n")
    with pytest.raises(OutcomeFileError) as exc:
        read_outcome_file(path)
    assert "subject" in str(exc.value)


def test_empty_file_is_refused(tmp_path):
    with pytest.raises(OutcomeFileError) as exc:
        read_outcome_file(_csv(tmp_path, ""))
    assert "empty" in str(exc.value)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_outcome_file(tmp_path / "nope.csv")


def test_bad_rows_are_reported_per_line_and_do_not_stop_the_file(tmp_path):
    path = _csv(
        tmp_path,
        "subject,label,score,observed_at\n"
        "alert:A-00,good,,\n"
        "alert:A-01,,,\n"                       # empty label
        "alert:A-02,bad_score,not-a-number,\n"  # unparseable score
        "alert:A-03,bad_time,,soon\n"           # unparseable timestamp
        ",no_key,,\n",                          # neither subject nor record id
    )
    rows, invalid = read_outcome_file(path)
    assert [r.subject for r in rows] == ["alert:A-00"]
    assert [line for line, _ in invalid] == [3, 4, 5, 6]
    assert "not a number" in invalid[1][1]
    assert "ISO 8601" in invalid[2][1]


# --- attaching --------------------------------------------------------------


def test_ingest_links_each_row_to_its_decision(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\nalert:A-01,reopened\n")
    report = ingest_outcomes([path], store, stream="aml", source="case-system")
    assert report.written == 2 and report.matched == 2 and report.unmatched == []
    attached = store.latest_outcome("01K5A3Q7Z2XV8M9N4B6C1D0E00")
    assert attached["outcome"]["label"] == "stayed_closed"
    assert attached["outcome"]["source"] == "case-system"
    assert attached["references"]["decision_record_id"] == "01K5A3Q7Z2XV8M9N4B6C1D0E00"


def test_re_ingesting_the_same_file_writes_nothing(tmp_path, store):
    path = _csv(tmp_path, "subject,label,observed_at\nalert:A-00,stayed_closed,2026-09-01T00:00:00Z\n")
    first = ingest_outcomes([path], store, stream="aml")
    second = ingest_outcomes([path], store, stream="aml")
    assert first.written == 1
    assert second.written == 0 and second.duplicates == 1
    assert second.matched == 1  # it still matched; it simply had nothing new to say


def test_a_corrected_label_is_a_new_record_and_the_later_one_wins(tmp_path, store):
    first = _csv(tmp_path, "subject,label,observed_at\nalert:A-00,reopened,2026-09-01T00:00:00Z\n", "a.csv")
    second = _csv(tmp_path, "subject,label,observed_at\nalert:A-00,stayed_closed,2026-09-02T00:00:00Z\n", "b.csv")
    ingest_outcomes([first], store, stream="aml")
    ingest_outcomes([second], store, stream="aml")
    linked = [
        r for r in store.iter_records("aml")
        if r["record_type"] == "outcome" and r["references"]["decision_record_id"].endswith("E00")
    ]
    assert len(linked) == 2  # append-only: the correction is an addition, both survive
    assert store.latest_outcome("01K5A3Q7Z2XV8M9N4B6C1D0E00")["outcome"]["label"] == "stayed_closed"


def test_rows_matching_no_decision_are_reported_not_raised(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,ok\nalert:NOPE,ok\n")
    report = ingest_outcomes([path], store, stream="aml")
    assert report.written == 1
    assert report.unmatched == [(3, "subject:alert:NOPE")]


def test_ingest_by_decision_record_id(tmp_path, store):
    path = _csv(tmp_path, "decision_record_id,label\n01K5A3Q7Z2XV8M9N4B6C1D0E02,reopened\n")
    report = ingest_outcomes([path], store, stream="aml")
    assert report.written == 1
    assert store.latest_outcome("01K5A3Q7Z2XV8M9N4B6C1D0E02")["outcome"]["label"] == "reopened"


def test_a_record_id_that_is_not_a_decision_does_not_match(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,ok\n")
    ingest_outcomes([path], store, stream="aml")
    outcome_id = store.latest_outcome("01K5A3Q7Z2XV8M9N4B6C1D0E00")["record_id"]
    pointed_at_an_outcome = _csv(tmp_path, f"decision_record_id,label\n{outcome_id},ok\n", "b.csv")
    report = ingest_outcomes([pointed_at_an_outcome], store, stream="aml")
    assert report.written == 0 and len(report.unmatched) == 1


def test_dry_run_writes_nothing_but_still_reports(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\n")
    report = ingest_outcomes([path], store, stream="aml", dry_run=True)
    assert report.matched == 1 and report.written == 0 and report.dry_run
    assert store.latest_outcome("01K5A3Q7Z2XV8M9N4B6C1D0E00") is None


def test_ingest_with_no_files_is_a_value_error(store):
    with pytest.raises(ValueError):
        ingest_outcomes([], store, stream="aml")


def test_imported_and_live_decisions_are_counted_separately(tmp_path):
    s = SQLiteStore(tmp_path / "mixed.db")
    s.write([_decision(0), _decision(1, origin="imported")])
    path = _csv(tmp_path, "subject,label\nalert:A-00,x\nalert:A-01,y\n")
    report = ingest_outcomes([path], s, stream="aml")
    assert report.matched_live == 1 and report.matched_imported == 1
    # the retroactive half inherits the decision's origin, so it stays distinguishable
    assert store_origin(s, "01K5A3Q7Z2XV8M9N4B6C1D0E01") == "imported"
    s.close()


def store_origin(store, decision_record_id):
    return store.latest_outcome(decision_record_id)["origin"]


def test_attached_outcomes_seal_and_verify_offline(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\nalert:A-01,reopened\n")
    ingest_outcomes([path], store, stream="aml")
    reports = verify_records(list(store.iter_records("aml")))
    assert [(r.stream, r.records, r.ok) for r in reports] == [("demo-bank/aml", 6, True)]


def test_build_outcome_record_is_deterministic():
    from warrant.outcomes import OutcomeRow

    decision = _decision(0)
    row = OutcomeRow(line=2, label="stayed_closed", subject="alert:A-00", observed_at="2026-09-01T00:00:00.000Z")
    first = build_outcome_record(decision, row)
    second = build_outcome_record(decision, row)
    assert first["record_id"] == second["record_id"]
    assert first["record_id"] != build_outcome_record(decision, OutcomeRow(line=2, label="reopened", subject="alert:A-00", observed_at="2026-09-01T00:00:00.000Z"))["record_id"]


# --- coverage ---------------------------------------------------------------


def test_coverage_counts_the_attached_share_overall_and_per_class(tmp_path):
    s = SQLiteStore(tmp_path / "cov.db")
    s.write([_decision(0), _decision(1), _decision(2, cls="aml.alert.escalation")])
    path = _csv(tmp_path, "subject,label\nalert:A-00,x\n")
    ingest_outcomes([path], s, stream="aml")
    report = coverage(s, stream="aml")
    assert (report.decisions, report.with_outcome) == (3, 1)
    assert report.share == pytest.approx(1 / 3)
    assert report.by_class["aml.alert.disposition"] == (2, 1)
    assert report.by_class["aml.alert.escalation"] == (1, 0)
    s.close()


def test_coverage_of_an_empty_stream_is_zero_not_a_crash(tmp_path):
    s = SQLiteStore(tmp_path / "empty.db")
    report = coverage(s, stream="nothing")
    assert report.decisions == 0 and report.share == 0.0
    assert "0/0" in report.summary()
    s.close()


def test_iter_joined_merges_the_latest_outcome(tmp_path, store):
    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\n")
    ingest_outcomes([path], store, stream="aml")
    joined = {r["decision"]["subject"]: r for r in iter_joined(store, "aml")}
    assert joined["alert:A-00"]["outcome"]["label"] == "stayed_closed"
    assert "outcome" not in joined["alert:A-01"]


# --- CLI --------------------------------------------------------------------


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "warrant.cli", *args], capture_output=True, text=True)


def test_cli_ingest_and_status(tmp_path):
    db = tmp_path / "cli.db"
    s = SQLiteStore(db)
    s.write([_decision(i) for i in range(4)])
    s.close()
    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\nalert:A-01,reopened\n")
    report_path = tmp_path / "report.json"

    ingest = _cli("outcomes", "ingest", str(path), "--store", str(db), "--stream", "aml", "--report", str(report_path))
    assert ingest.returncode == 0, ingest.stderr
    assert "wrote 2 outcome record(s)" in ingest.stdout
    assert json.loads(report_path.read_text())["coverage"]["share"] == 0.5

    status = _cli("outcomes", "status", "--store", str(db), "--stream", "aml", "--json")
    assert status.returncode == 0
    assert json.loads(status.stdout)["with_outcome"] == 2


def test_cli_reports_a_missing_store_rather_than_creating_one(tmp_path):
    result = _cli("outcomes", "status", "--store", str(tmp_path / "absent.db"))
    assert result.returncode == 1
    assert "no store there" in result.stderr
    assert not (tmp_path / "absent.db").exists()


def test_iter_joined_agrees_with_the_join_warrant_set_uses(tmp_path, store):
    """The docstring claims these two cannot drift on what 'the outcome of a decision' means.

    They are separate code paths, so assert it rather than trust it: a change to either that
    altered the merge would make calibration and replay disagree about the same decision.
    """
    from warrant.sets import build_set

    path = _csv(tmp_path, "subject,label\nalert:A-00,stayed_closed\nalert:A-02,reopened\n")
    ingest_outcomes([path], store, stream="aml")

    mine = {
        r["decision"]["subject"]: (r.get("outcome") or {}).get("label")
        for r in iter_joined(store, "aml")
    }
    theirs = {
        item.record["decision"]["subject"]: item.outcome_label
        for item in build_set(store, "check", stream="aml").items
    }
    assert mine == theirs == {
        "alert:A-00": "stayed_closed",
        "alert:A-01": None,
        "alert:A-02": "reopened",
        "alert:A-03": None,
    }


# --- tenants sharing a stream name ---------------------------------------------


@pytest.fixture
def shared(tmp_path):
    """Two tenants, the same stream name, the same subjects: what a shared collector store holds."""
    s = SQLiteStore(tmp_path / "shared.db")
    theirs = [dict(_decision(i), tenant="other-bank", record_id=f"01K5A3Q7Z2XV8M9N4B6C1D1E{i:02d}") for i in range(2)]
    s.write([_decision(i) for i in range(2)] + theirs)
    yield s
    s.close()


def test_a_shared_stream_is_not_ingested_until_the_tenant_is_named(tmp_path, shared):
    path = _csv(tmp_path, "subject,label\nalert:A-00,reopened\n")
    with pytest.raises(ValueError, match="holds several tenants"):
        ingest_outcomes([path], shared, stream="aml")
    assert shared.count() == 4


def test_an_outcome_attaches_to_the_named_tenants_decision_only(tmp_path, shared):
    path = _csv(tmp_path, "subject,label\nalert:A-00,reopened\n")
    report = ingest_outcomes([path], shared, stream="aml", tenant="demo-bank")
    assert report.written == 1 and report.duplicates == 0
    outcome = [r for r in shared.iter_records("aml") if r["record_type"] == "outcome"]
    assert [(r["tenant"], r["references"]["decision_record_id"]) for r in outcome] == [("demo-bank", "01K5A3Q7Z2XV8M9N4B6C1D0E00")]
    assert report.coverage.decisions == 2 and report.coverage.with_outcome == 1
    assert coverage(shared, stream="aml", tenant="other-bank").with_outcome == 0
    assert [r["tenant"] for r in iter_joined(shared, "aml", "other-bank")] == ["other-bank", "other-bank"]
    again = ingest_outcomes([path], shared, stream="aml", tenant="demo-bank")
    assert again.written == 0 and again.duplicates == 1


def test_a_record_id_from_another_tenant_matches_nothing(tmp_path, shared):
    path = _csv(tmp_path, "decision_record_id,label\n01K5A3Q7Z2XV8M9N4B6C1D1E00,reopened\n")
    report = ingest_outcomes([path], shared, stream="aml", tenant="demo-bank")
    assert report.written == 0 and len(report.unmatched) == 1


def test_sets_packs_and_breakers_read_one_tenant_of_a_shared_stream(tmp_path, shared):
    from warrant.breaker import count_window
    from warrant.pack import build_pack
    from warrant.sets import build_set

    for read in (
        lambda **kw: build_set(shared, "s", stream="aml", **kw),
        lambda **kw: build_pack(shared, tmp_path / "pack", stream="aml", **kw),
        lambda **kw: count_window(shared, "aml.alert.disposition", "2026-01-01T00:00:00Z", stream="aml", **kw),
    ):
        with pytest.raises(ValueError, match="holds several tenants"):
            read()
    assert len(build_set(shared, "s", stream="aml", tenant="other-bank").items) == 2
    assert count_window(shared, "aml.alert.disposition", "2026-01-01T00:00:00Z", stream="aml", tenant="demo-bank").decisions == 2
    build_pack(shared, tmp_path / "pack", stream="aml", tenant="other-bank")
    packed = [json.loads(line) for line in (tmp_path / "pack" / "records.jsonl").read_text().splitlines()]
    assert {r["tenant"] for r in packed} == {"other-bank"} and len(packed) == 2
