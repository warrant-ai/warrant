import json
import sqlite3

import pytest

from warrant import SQLiteStore, verify_records
from warrant.emit import PermanentSinkError
from warrant.hashing import record_hash


def _unsealed(i, stream="s"):
    return {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D0E{i:02d}",
        "record_type": "decision",
        "tenant": "t",
        "stream": stream,
        "timestamp": "2026-09-16T10:00:00Z",
        "schema_version": "0",
        "origin": "live",
        "actor": {"name": "a", "version": "1"},
        "decision": {"class": "x.y", "action": "do", "subject": f"S{i}", "status": "acted"},
        "mandate": {"result": "unchecked"},
    }


def test_store_seals_chains_and_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.write([_unsealed(1), _unsealed(2)])
    store.write([_unsealed(2), _unsealed(3)])
    records = list(store.iter_records("s"))
    assert [r["sequence"] for r in records] == [1, 2, 3]
    assert records[0]["seal"]["prev_hash"] is None
    assert records[1]["seal"]["prev_hash"] == records[0]["seal"]["hash"]
    assert records[2]["seal"]["hash"] == record_hash(records[2], records[1]["seal"]["hash"])
    assert store.find_decision("s", "S2") == records[1]["record_id"]
    assert store.get(records[0]["record_id"]) == records[0]
    assert store.streams() == ["s"] and store.count("s") == 3
    store.close()


def test_store_rejects_invalid_and_presealed_records_permanently(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    bad = _unsealed(1)
    del bad["actor"]
    with pytest.raises(PermanentSinkError, match="schema validation"):
        store.write([bad])
    sealed = dict(_unsealed(2), seal={"prev_hash": None, "hash": "0" * 64})
    with pytest.raises(PermanentSinkError, match="already sealed"):
        store.write([sealed])
    assert store.count() == 0
    store.close()


def test_store_refuses_update_and_delete(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.write([_unsealed(1)])
    store.close()
    conn = sqlite3.connect(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE records SET body = '{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM records")
    conn.close()


def test_verifier_passes_clean_chain_and_detects_tampering(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.write([_unsealed(i) for i in range(1, 6)])
    store.write([_unsealed(9, stream="other")])
    records = list(store.iter_records())
    store.close()

    reports = verify_records(records)
    assert [(r.stream, r.records, r.ok) for r in reports] == [("t/other", 1, True), ("t/s", 5, True)]

    tampered = json.loads(json.dumps(records))
    tampered[3]["decision"]["action"] = "undo"  # records are ordered by stream, so index 3 is s #3
    (_, report) = verify_records(tampered)
    assert not report.ok and any("hash mismatch" in e and "#3" in e for e in report.errors)

    missing = [r for r in records if not (r["stream"] == "s" and r["sequence"] == 2)]
    (_, report) = verify_records(missing)
    assert not report.ok and any("expected sequence 2" in e for e in report.errors)
    assert any("prev_hash does not match" in e for e in report.errors)

    unsealed = json.loads(json.dumps(records))
    del unsealed[0]["seal"]
    (report, _) = verify_records(unsealed)
    assert not report.ok and "has no seal" in report.errors[0]


def test_verifier_flags_records_without_stream_or_sequence():
    (report,) = verify_records([{"record_id": "x"}])
    assert report.stream == "(no stream)" and not report.ok
    (report,) = verify_records([{"record_id": "x", "stream": "s", "tenant": "t"}])
    assert "sequence" in report.errors[0]
    (report,) = verify_records([{"record_id": "x", "stream": "s"}])
    assert report.stream == "(no stream)"  # no tenant, no chain
