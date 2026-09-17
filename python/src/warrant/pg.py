"""PostgreSQL store for self-hosted and hosted deployments. Same contract as ``SQLiteStore``.

Chaining is serialised per (tenant, stream) with a row lock on ``stream_heads``, so any
number of stateless collector instances can write concurrently. Records are
append-only: triggers refuse UPDATE and DELETE. Requires ``pip install "warrantai[collector]"``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Iterator, List, Optional, Sequence

from warrant.emit import PermanentSinkError
from warrant.hashing import canonical_json, record_hash
from warrant.schema import ValidationError, validate

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover
    raise ImportError('the PostgreSQL store needs psycopg: pip install "warrantai[collector]"') from exc

log = logging.getLogger("warrant.pg")

_DDL = """
CREATE TABLE IF NOT EXISTS records (
    seq BIGSERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    stream TEXT NOT NULL,
    stream_seq BIGINT NOT NULL,
    record_id TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL,
    subject TEXT,
    decision_record_id TEXT,
    ts TIMESTAMPTZ NOT NULL,
    prev_hash TEXT,
    hash TEXT NOT NULL UNIQUE,
    body JSONB NOT NULL,
    UNIQUE (tenant, stream, stream_seq)
);
CREATE INDEX IF NOT EXISTS records_subject ON records (tenant, stream, subject, record_type);
CREATE INDEX IF NOT EXISTS records_decision ON records (decision_record_id);
CREATE INDEX IF NOT EXISTS records_ts ON records (tenant, stream, ts);
CREATE TABLE IF NOT EXISTS evidence_blobs (
    hash TEXT PRIMARY KEY,
    blob JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_heads (
    tenant TEXT NOT NULL,
    stream TEXT NOT NULL,
    stream_seq BIGINT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (tenant, stream)
);
CREATE OR REPLACE FUNCTION warrant_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'warrant records are append-only';
END;
$$ LANGUAGE plpgsql;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'records_no_update') THEN
        CREATE TRIGGER records_no_update BEFORE UPDATE ON records FOR EACH ROW EXECUTE FUNCTION warrant_append_only();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'records_no_delete') THEN
        CREATE TRIGGER records_no_delete BEFORE DELETE ON records FOR EACH ROW EXECUTE FUNCTION warrant_append_only();
    END IF;
END $$;
"""

_SETUP_LOCK = 7_318_204_119  # arbitrary advisory lock key for schema setup


class PostgresStore:
    def __init__(self, dsn: str, *, read_only: bool = False) -> None:
        self.dsn = dsn
        self.path = dsn.split("@")[-1]  # host/db only, for logs
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        if read_only:
            self._conn.read_only = True
        else:
            self._setup()
        log.info("warrant postgres store connected to %s%s", self.path, " (read-only)" if read_only else "")

    def _setup(self) -> None:
        """Create tables and triggers once. Serialised across instances with an advisory lock so
        concurrent start-ups never take exclusive table locks against each other's writes."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT to_regclass('records') IS NOT NULL AS present, EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'records_no_delete') AS triggers")
            row = cur.fetchone()
            if row["present"] and row["triggers"]:
                self._conn.rollback()
                return
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SETUP_LOCK,))
            cur.execute(_DDL)
        self._conn.commit()

    # -- Sink ----------------------------------------------------------------

    def write(self, records: Sequence[Dict[str, Any]]) -> None:
        """Seal and insert a batch in one transaction. Already-stored record_ids are skipped."""
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    for record in records:
                        self._insert(cur, record)
                self._conn.commit()
            except PermanentSinkError:
                self._conn.rollback()
                raise
            except psycopg.Error as exc:
                self._conn.rollback()
                raise psycopg.OperationalError(f"warrant postgres write failed: {exc}") from exc

    def _insert(self, cur, record: Dict[str, Any]) -> None:
        record_id = record.get("record_id")
        if not isinstance(record_id, str):
            raise PermanentSinkError("record has no record_id")
        if "seal" in record:
            raise PermanentSinkError(f"record {record_id} is already sealed; the store seals records itself")
        blobs = record.get("_blobs") or {}
        if blobs:
            record = {k: v for k, v in record.items() if k != "_blobs"}
            for digest, blob in blobs.items():
                cur.execute("INSERT INTO evidence_blobs (hash, blob) VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING", (digest, canonical_json(blob)))
        cur.execute("SELECT 1 FROM records WHERE record_id = %s", (record_id,))
        if cur.fetchone():
            log.info("warrant postgres store skipping duplicate record %s", record_id)
            return
        stream, tenant = record.get("stream"), record.get("tenant")
        if not isinstance(stream, str) or not stream:
            raise PermanentSinkError(f"record {record_id} has no stream")
        if not isinstance(tenant, str) or not tenant:
            raise PermanentSinkError(f"record {record_id} has no tenant")
        cur.execute("INSERT INTO stream_heads (tenant, stream, stream_seq, hash) VALUES (%s, %s, 0, '') ON CONFLICT DO NOTHING", (tenant, stream))
        cur.execute("SELECT stream_seq, hash FROM stream_heads WHERE tenant = %s AND stream = %s FOR UPDATE", (tenant, stream))
        head = cur.fetchone()
        stream_seq = head["stream_seq"] + 1
        prev_hash = head["hash"] or None

        sealed = dict(record)
        sealed["sequence"] = stream_seq
        sealed["seal"] = {"prev_hash": prev_hash, "hash": record_hash(sealed, prev_hash)}
        try:
            validate(sealed)
        except ValidationError as exc:
            raise PermanentSinkError(f"record {record_id} failed schema validation: {exc.errors[0]}") from exc
        cur.execute(
            "INSERT INTO records (tenant, stream, stream_seq, record_id, record_type, subject, decision_record_id,"
            " ts, prev_hash, hash, body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                tenant, stream, stream_seq, record_id, sealed["record_type"],
                (sealed.get("decision") or {}).get("subject"),
                (sealed.get("references") or {}).get("decision_record_id"),
                sealed["timestamp"], prev_hash, sealed["seal"]["hash"], canonical_json(sealed),
            ),
        )
        cur.execute("UPDATE stream_heads SET stream_seq = %s, hash = %s WHERE tenant = %s AND stream = %s", (stream_seq, sealed["seal"]["hash"], tenant, stream))

    # -- queries -------------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            self._conn.rollback()  # end the read transaction
        return rows

    def iter_records(self, stream: Optional[str] = None, tenant: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        sql = "SELECT body FROM records"
        clauses, params = [], []
        if stream is not None:
            clauses.append("stream = %s")
            params.append(stream)
        if tenant is not None:
            clauses.append("tenant = %s")
            params.append(tenant)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY tenant, stream, stream_seq"
        for row in self._query(sql, tuple(params)):
            yield _body(row)

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT body FROM records WHERE record_id = %s", (record_id,))
        return _body(rows[0]) if rows else None

    def find_decision(self, stream: str, subject: str, tenant: Optional[str] = None) -> Optional[str]:
        sql = "SELECT record_id FROM records WHERE stream = %s AND subject = %s AND record_type = 'decision'"
        params: list = [stream, subject]
        if tenant is not None:
            sql += " AND tenant = %s"
            params.append(tenant)
        sql += " ORDER BY seq DESC LIMIT 1"
        rows = self._query(sql, tuple(params))
        return rows[0]["record_id"] if rows else None

    def get_blob(self, content_hash: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT blob FROM evidence_blobs WHERE hash = %s", (content_hash,))
        if not rows:
            return None
        blob = rows[0]["blob"]
        return blob if isinstance(blob, dict) else json.loads(blob)

    def latest_outcome(self, decision_record_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT body FROM records WHERE decision_record_id = %s AND record_type = 'outcome' ORDER BY seq DESC LIMIT 1", (decision_record_id,))
        return _body(rows[0]) if rows else None

    def streams(self) -> List[str]:
        return [r["stream"] for r in self._query("SELECT DISTINCT stream FROM stream_heads ORDER BY stream")]

    def count(self, stream: Optional[str] = None) -> int:
        if stream is None:
            return self._query("SELECT COUNT(*) AS n FROM records")[0]["n"]
        return self._query("SELECT COUNT(*) AS n FROM records WHERE stream = %s", (stream,))[0]["n"]

    def ping(self) -> bool:
        try:
            self._query("SELECT 1")
            return True
        except psycopg.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _body(row: Dict[str, Any]) -> Dict[str, Any]:
    body = row["body"]
    return body if isinstance(body, dict) else json.loads(body)
