import json
import os
import socket
import sqlite3
import threading
import time

import pytest

from warrant import AgentInfo, SQLiteStore, Warrant, verify_records
from warrant.emit import PermanentSinkError, SinkError
from warrant.store import open_store

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from warrant.collector import create_app, parse_tokens  # noqa: E402
from warrant.sinks import HttpSink  # noqa: E402

PG_DSN = os.environ.get("WARRANT_TEST_PG", "postgresql://warrant:warrant@127.0.0.1:55432/warrant")
TOKEN = "demo-bank-secret-token-0123456789"


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


def _unsealed(i, tenant="demo-bank", stream="lending", subject=None):
    return {
        "record_id": f"01K5TEST{i:018d}",  # deterministic per i, valid ULID alphabet
        "record_type": "decision", "tenant": tenant, "stream": stream,
        "timestamp": "2026-09-17T10:00:00Z", "schema_version": "0", "origin": "live",
        "actor": {"name": "a", "version": "1"},
        "decision": {"class": "credit.approve", "action": "approve", "subject": subject or f"LN-{i}", "status": "acted"},
        "mandate": {"result": "unchecked"},
    }


# -- store factory and migration ----------------------------------------------


def test_open_store_routes_by_url(tmp_path):
    s = open_store(tmp_path / "x.db")
    assert isinstance(s, SQLiteStore)
    s.close()


def test_sqlite_chains_are_per_tenant(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.write([_unsealed(1, tenant="a"), _unsealed(2, tenant="b"), _unsealed(3, tenant="a")])
    a = list(store.iter_records("lending", tenant="a"))
    b = list(store.iter_records("lending", tenant="b"))
    assert [r["sequence"] for r in a] == [1, 2] and [r["sequence"] for r in b] == [1]
    assert a[1]["seal"]["prev_hash"] == a[0]["seal"]["hash"] and b[0]["seal"]["prev_hash"] is None
    reports = verify_records(list(store.iter_records()))
    assert [(r.stream, r.ok) for r in reports] == [("a/lending", True), ("b/lending", True)]
    assert store.find_decision("lending", "LN-2", tenant="a") is None and store.find_decision("lending", "LN-2", tenant="b")
    store.close()


def test_migrates_a_0_1_0_store(tmp_path):
    """A store written by 0.1.0 (chains per stream, no tenant column) opens and keeps chaining."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE records (seq INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, stream_seq INTEGER NOT NULL,
            record_id TEXT NOT NULL UNIQUE, record_type TEXT NOT NULL, subject TEXT, decision_record_id TEXT,
            timestamp TEXT NOT NULL, prev_hash TEXT, hash TEXT NOT NULL UNIQUE, body TEXT NOT NULL, UNIQUE (stream, stream_seq));
        CREATE TABLE stream_heads (stream TEXT PRIMARY KEY, stream_seq INTEGER NOT NULL, hash TEXT NOT NULL);
        CREATE TRIGGER records_no_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'append-only'); END;
    """)
    conn.close()
    # write two records through the old layout by using the new store on a fresh db, then copying rows across
    fresh = SQLiteStore(tmp_path / "fresh.db")
    fresh.write([_unsealed(1, tenant="demo-bank"), _unsealed(2, tenant="demo-bank")])
    rows = list(fresh.iter_records())
    fresh.close()
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute("INSERT INTO records (stream, stream_seq, record_id, record_type, subject, timestamp, prev_hash, hash, body) VALUES (?,?,?,?,?,?,?,?,?)",
                     (r["stream"], r["sequence"], r["record_id"], r["record_type"], r["decision"]["subject"], r["timestamp"], r["seal"]["prev_hash"], r["seal"]["hash"], json.dumps(r)))
    conn.execute("INSERT INTO stream_heads VALUES (?,?,?)", (rows[-1]["stream"], rows[-1]["sequence"], rows[-1]["seal"]["hash"]))
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.write([_unsealed(3, tenant="demo-bank")])
    migrated = list(store.iter_records("lending", tenant="demo-bank"))
    assert [r["sequence"] for r in migrated] == [1, 2, 3]
    assert migrated[2]["seal"]["prev_hash"] == migrated[1]["seal"]["hash"]
    (report,) = verify_records(migrated)
    assert report.ok, report.errors
    store.close()


# -- PostgreSQL ------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available(), reason="no PostgreSQL at WARRANT_TEST_PG")
class TestPostgres:
    @pytest.fixture
    def pg(self):
        import psycopg

        from warrant.pg import PostgresStore

        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS records, stream_heads, evidence_blobs CASCADE")
        store = PostgresStore(PG_DSN)
        yield store
        store.close()

    def test_seals_chains_dedupes_and_refuses_edits(self, pg):
        import psycopg

        pg.write([_unsealed(1), _unsealed(2)])
        pg.write([_unsealed(2), _unsealed(3, tenant="other")])
        recs = list(pg.iter_records())
        assert [(r["tenant"], r["sequence"]) for r in recs] == [("demo-bank", 1), ("demo-bank", 2), ("other", 1)]
        assert recs[1]["seal"]["prev_hash"] == recs[0]["seal"]["hash"]
        assert all(r.ok for r in verify_records(recs))
        assert pg.count() == 3 and pg.streams() == ["lending"] and pg.get(recs[0]["record_id"]) == recs[0]
        assert pg.find_decision("lending", "LN-2", tenant="demo-bank") == recs[1]["record_id"]
        assert pg.ping()
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            with pytest.raises(psycopg.Error, match="append-only"):
                conn.execute("UPDATE records SET body = '{}'::jsonb")
            with pytest.raises(psycopg.Error, match="append-only"):
                conn.execute("DELETE FROM records")
        bad = _unsealed(9)
        del bad["actor"]
        with pytest.raises(PermanentSinkError, match="schema validation"):
            pg.write([bad])
        assert pg.count() == 3

    def test_blobs_outcomes_and_concurrent_writers(self, pg):
        from warrant.pg import PostgresStore

        with Warrant("lending", tenant="demo-bank", store=PG_DSN, agent=AgentInfo("a", "1"), capture_inputs=True, capture_evidence=True, flush_interval=0.02) as w:
            with w.decide("credit.approve", subject="LN-1") as d:
                d.check(amount=1)
                d.tool("bureau", lambda: {"score": 700})
                d.act("approve")
            w.outcome(subject="LN-1", label="performing")
            assert w.flush()
        rec = list(pg.iter_records("lending"))[0]
        assert pg.get_blob(rec["evidence"][0]["content_hash"]) == {"encoding": "json", "data": {"score": 700}}
        assert pg.latest_outcome(rec["record_id"])["outcome"]["label"] == "performing"

        def writer(n):
            s = PostgresStore(PG_DSN)
            for i in range(20):
                s.write([_unsealed(1000 * n + i, subject=f"C-{n}-{i}")])
            s.close()

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = list(pg.iter_records("lending", tenant="demo-bank"))
        assert [r["sequence"] for r in recs] == list(range(1, 83))
        (report,) = verify_records(recs)
        assert report.ok, report.errors


# -- collector ---------------------------------------------------------------------


def test_parse_tokens():
    assert parse_tokens("demo-bank:" + TOKEN + ", other-co:other-co-token-abcdefghijklmnop") == {TOKEN: "demo-bank", "other-co-token-abcdefghijklmnop": "other-co"}
    assert parse_tokens("") == {}
    with pytest.raises(ValueError, match="tenant:token"):
        parse_tokens("nocolon")
    with pytest.raises(ValueError, match="16 characters"):
        parse_tokens("t:short")


@pytest.fixture
def collector(tmp_path):
    store = SQLiteStore(tmp_path / "c.db")
    app = create_app(store, tokens={TOKEN: "demo-bank"})
    client = TestClient(app)
    yield client, store
    store.close()


def _post(client, records, token=TOKEN, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/v1/records", json={"records": records}, headers=headers, **kw)


def test_collector_accepts_seals_and_dedupes(collector):
    client, store = collector
    r = _post(client, [_unsealed(1), _unsealed(2)])
    assert r.status_code == 202 and r.json() == {"accepted": 2, "duplicates": 0}
    assert "x-request-id" in r.headers
    recs = list(store.iter_records("lending"))
    assert [x["sequence"] for x in recs] == [1, 2] and "seal" in recs[0]
    r = _post(client, [_unsealed(2)])
    assert r.status_code == 202 and r.json() == {"accepted": 0, "duplicates": 1}
    assert client.get("/healthz").json()["status"] == "ok" and client.get("/readyz").status_code == 200
    text = client.get("/metrics").text
    assert "warrant_collector_accepted_total 2" in text and "warrant_collector_duplicates_total 1" in text


def test_collector_rejections(collector):
    client, _ = collector
    assert _post(client, [_unsealed(1)], token=None).status_code == 401
    assert _post(client, [_unsealed(1)], token="wrong-token-abcdefghijklmnop").status_code == 401
    r = _post(client, [_unsealed(1, tenant="other-co")])
    assert r.status_code == 403 and "tenant" in r.json()["problems"][0]["error"]
    bad = _unsealed(1)
    del bad["decision"]
    r = _post(client, [bad, "junk"])
    assert r.status_code == 400 and len(r.json()["problems"]) == 2
    sealed = _unsealed(1) | {"seal": {"prev_hash": None, "hash": "0" * 64}}
    assert _post(client, [sealed]).status_code == 400
    assert client.post("/v1/records", content=b"{not json", headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}).status_code == 400
    assert _post(client, []).status_code == 400
    assert _post(client, [_unsealed(i) for i in range(1001)]).status_code == 413
    assert client.post("/v1/records", content=b"\x1f\x8bgarbage", headers={"Authorization": f"Bearer {TOKEN}", "Content-Encoding": "gzip"}).status_code == 400
    assert "warrant_collector_auth_failures_total 2" in client.get("/metrics").text


def test_collector_reports_store_outage(tmp_path):
    class Down:
        def get(self, rid):
            return None

        def write(self, records):
            raise ConnectionError("db down")

        def ping(self):
            return False

    client = TestClient(create_app(Down(), tokens={TOKEN: "demo-bank"}))
    assert client.get("/readyz").status_code == 503
    assert _post(client, [_unsealed(1)]).status_code == 503
    with pytest.raises(ValueError, match="no tokens"):
        create_app(Down())


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_collector(tmp_path):
    import uvicorn

    store = SQLiteStore(tmp_path / "live.db")
    app = create_app(store, tokens={TOKEN: "demo-bank"})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", store
    server.should_exit = True
    thread.join(5)
    store.close()


def test_sdk_to_collector_end_to_end(live_collector, tmp_path):
    url, store = live_collector
    with Warrant("lending", tenant="demo-bank", store=url, token=TOKEN, agent=AgentInfo("credit-underwriter", "2.3.1"), spill_dir=tmp_path / "spill", flush_interval=0.02) as w:
        assert w.store is None
        for i in range(250):  # more than one batch, and big enough to gzip
            with w.decide("credit.approve", subject=f"LN-{i}") as d:
                d.evidence("bureau", uri="cibil://x", content={"i": i}, excerpt="x" * 40)
                d.act("approve")
        assert w.flush(timeout=20)
        with pytest.raises(LookupError, match="decision_record_id"):
            w.outcome(subject="LN-1", label="x")
        w.outcome(decision_record_id=list(store.iter_records("lending"))[0]["record_id"], label="performing")
        assert w.flush(timeout=10)
        assert w.stats()["delivered"] == 251 and w.stats()["spilled"] == 0
    recs = list(store.iter_records("lending"))
    assert len(recs) == 251 and [r["sequence"] for r in recs] == list(range(1, 252))
    (report,) = verify_records(recs)
    assert report.ok, report.errors


def test_http_sink_error_classes(live_collector, tmp_path):
    url, _ = live_collector
    sink = HttpSink(url, "wrong-token-abcdefghijklmnop", compress=False)
    with pytest.raises(PermanentSinkError, match="401"):
        sink.write([_unsealed(1)])
    sink = HttpSink(url, TOKEN)
    with pytest.raises(PermanentSinkError, match="tenant"):
        sink.write([_unsealed(1, tenant="other-co")])
    dead = HttpSink(f"http://127.0.0.1:{_free_port()}", TOKEN, timeout=1)
    with pytest.raises(SinkError, match="unreachable"):
        dead.write([_unsealed(1)])
    with pytest.raises(ValueError):
        HttpSink("ftp://x", TOKEN)


def test_sdk_spills_when_collector_is_down_and_recovers(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    w = Warrant("lending", tenant="demo-bank", store=url, token=TOKEN, agent=AgentInfo("a", "1"), spill_dir=tmp_path / "spill", flush_interval=0.02)
    with w.decide("credit.approve", subject="LN-1") as d:
        d.act("approve")
    assert w.flush(timeout=10)
    assert w.stats()["spilled"] == 1 and list((tmp_path / "spill").glob("*.jsonl"))
    import uvicorn

    store = SQLiteStore(tmp_path / "late.db")
    server = uvicorn.Server(uvicorn.Config(create_app(store, tokens={TOKEN: "demo-bank"}), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and store.count() < 1:
        time.sleep(0.1)
    assert store.count() == 1
    w.close()
    server.should_exit = True
    thread.join(5)
    store.close()


@pytest.mark.skipif(not _pg_available(), reason="no PostgreSQL at WARRANT_TEST_PG")
def test_postgres_signs_erases_and_carries_the_lifecycle(tmp_path):
    """The ADR path end to end on PostgreSQL: signed seals, a salted sidecar erased, a transition."""
    pytest.importorskip("cryptography")
    import psycopg

    from warrant import AgentInfo, Warrant
    from warrant.signing import Keyring, SigningKey

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS records, stream_heads, evidence_blobs CASCADE")
    key = SigningKey.generate("demo-bank")
    w = Warrant("lending", tenant="demo-bank", store=PG_DSN, signing_key=key, agent=AgentInfo("credit-agent", "1"), flush_interval=0.02)
    try:
        with w.decide("credit.approve", subject="LN-1") as d:
            d.obligation("OB-1", requires="human_review")
            shown = d.evidence("statement", uri="doc://1", type="document", provider="bank-ops", content={"pan": "ABCDE1234F"}, sensitive=True)
        w.transition(d.record_id, "warranted", decided_by="human:officer", reviewer="officer", shown=[shown])
        w.flush()
        records = list(w.store.iter_records("lending"))
        assert [r["record_type"] for r in records] == ["decision", "transition"]
        assert all(r["seal"]["key_id"] == key.key_id for r in records)
        report = verify_records(records, keyring=Keyring([key.public]))[0]
        assert report.ok and report.level == "L2", report.warnings
        assert len(w.store.get_blob(shown)["salt"]) == 64
        assert w.store.erase_blob(shown) and w.store.get_blob(shown) is None
        assert verify_records(list(w.store.iter_records("lending")), keyring=Keyring([key.public]))[0].ok
    finally:
        w.close()


# -- tenants sharing a stream name ---------------------------------------------


def test_a_stream_two_tenants_share_is_never_read_without_naming_the_tenant(tmp_path):
    from warrant.store import resolve_tenant

    store = SQLiteStore(tmp_path / "shared.db")
    assert store.write([_unsealed(1, tenant="bank-a"), _unsealed(2, tenant="bank-b"), _unsealed(3, tenant="bank-a", stream="kyc")]) == 3
    assert store.write([_unsealed(1, tenant="bank-a"), _unsealed(4, tenant="bank-a")]) == 1
    assert store.tenants("lending") == ["bank-a", "bank-b"] and store.tenants("kyc") == ["bank-a"]
    assert store.tenants() == ["bank-a", "bank-b"] and store.tenants("absent") == []
    assert resolve_tenant(store, "kyc", None) == "bank-a" and resolve_tenant(store, "absent", None) is None
    assert resolve_tenant(store, "lending", "bank-b") == "bank-b"
    with pytest.raises(ValueError, match="holds several tenants \\(bank-a, bank-b\\)"):
        resolve_tenant(store, "lending", None)
    with pytest.raises(ValueError, match="the store holds several tenants"):
        resolve_tenant(store, None, None)
    store.close()


def test_export_refuses_a_shared_stream_until_the_tenant_is_named(tmp_path, capsys):
    from warrant.cli import main

    db = tmp_path / "shared.db"
    store = SQLiteStore(db)
    store.write([_unsealed(1, tenant="bank-a"), _unsealed(2, tenant="bank-b")])
    store.close()
    assert main(["export", "--store", str(db), "--stream", "lending"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "pass the tenant" in captured.err
    assert main(["export", "--store", str(db), "--stream", "lending", "--tenant", "bank-b"]) == 0
    exported = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["tenant"] for r in exported] == ["bank-b"]
    assert main(["export", "--store", str(db)]) == 0  # the whole store is the operator's own export
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_collector_counts_duplicates_inside_one_batch(collector):
    client, _ = collector
    r = _post(client, [_unsealed(1), _unsealed(1), _unsealed(2)])
    assert r.status_code == 202 and r.json() == {"accepted": 2, "duplicates": 1}
    assert "warrant_collector_duplicates_total 1" in client.get("/metrics").text


def test_collector_keeps_answering_while_a_batch_is_being_written(tmp_path):
    """A store write runs in the threadpool, so a slow one cannot stall health checks."""
    import asyncio

    import httpx

    entered, release = threading.Event(), threading.Event()

    class Slow:
        def write(self, records):
            entered.set()
            assert release.wait(5), "the health check never ran while the write was in progress"
            return len(records)

    app = create_app(Slow(), tokens={TOKEN: "demo-bank"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://collector") as client:
            write = asyncio.create_task(client.post("/v1/records", json={"records": [_unsealed(1)]}, headers={"Authorization": f"Bearer {TOKEN}"}))
            assert await asyncio.to_thread(entered.wait, 5)
            health = await asyncio.wait_for(client.get("/healthz"), timeout=2)
            release.set()
            return health, await write

    health, written = asyncio.run(scenario())
    assert health.status_code == 200 and written.json() == {"accepted": 1, "duplicates": 0}
