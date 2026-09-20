"""Local append-only store (SQLite) that seals records into a per-stream hash chain.

Records arrive unsealed. The store assigns the next ``sequence`` in the stream, sets
``seal.prev_hash`` to the stream head, computes ``seal.hash`` over the body, validates
the sealed record against the schema, and inserts it. Triggers refuse UPDATE and
DELETE so the file itself is append-only.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

from warrant.emit import PermanentSinkError
from warrant.hashing import canonical_json, record_hash
from warrant.schema import ValidationError, validate

log = logging.getLogger("warrant.store")

_DDL = """
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL DEFAULT 'local',
    stream TEXT NOT NULL,
    stream_seq INTEGER NOT NULL,
    record_id TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL,
    subject TEXT,
    decision_record_id TEXT,
    timestamp TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    UNIQUE (tenant, stream, stream_seq)
);
CREATE INDEX IF NOT EXISTS records_subject ON records (tenant, stream, subject, record_type);
CREATE INDEX IF NOT EXISTS records_decision ON records (decision_record_id);
CREATE TABLE IF NOT EXISTS evidence_blobs (
    hash TEXT PRIMARY KEY,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_heads (
    tenant TEXT NOT NULL,
    stream TEXT NOT NULL,
    stream_seq INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (tenant, stream)
);
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'warrant records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'warrant records are append-only'); END;
"""


#: A URL that has been through ``Path``: the scheme survives but ``//`` collapses to ``/``.
_MANGLED_URL = re.compile(r"^(postgres|postgresql|http|https):(?!//)")


def refuse_mangled_url(target: str) -> None:
    """Refuse a DSN or collector URL that has been through ``Path``, rather than writing locally.

    ``Path("postgresql://host/db")`` stringifies back as ``postgresql:/host/db`` — one slash, not
    two — so the scheme checks below miss it and the records go to a **local SQLite file named
    after the URL**. Nothing raises, nothing warns, and the ledger looks like it is working while
    every decision lands on one box that nobody is backing up or reading. That is the worst failure
    a decision ledger can have, so it fails loudly here instead.

    A wrapper this close to a silent data-loss bug does not try to repair the string: guessing at
    what the caller meant is how the mangling happened in the first place.
    """
    if _MANGLED_URL.match(target):
        scheme = target.split(":", 1)[0]
        raise ValueError(
            f"store={target!r} looks like a {scheme} URL that has been through pathlib.Path, "
            f"which turns '{scheme}://' into '{scheme}:/'. Left alone this would silently open a "
            "local SQLite file with that name instead of connecting, and every record would be "
            "written somewhere nobody is looking. Pass the URL as a plain string."
        )


def open_store(url: Union[str, Path], *, read_only: bool = False):
    """``postgres://...`` or ``postgresql://...`` opens a PostgreSQL store; anything else is a SQLite path."""
    text = str(url)
    refuse_mangled_url(text)
    if text.startswith(("postgres://", "postgresql://")):
        from warrant.pg import PostgresStore

        return PostgresStore(text, read_only=read_only)
    return SQLiteStore(url, read_only=read_only)


class SQLiteStore:
    """Append-only local store. Safe to share between the emitter thread and callers."""

    def __init__(self, path: Union[str, Path], *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(f"warrant store not found: {self.path}")
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate_v01()
            self._conn.executescript(_DDL)
        self._conn.row_factory = sqlite3.Row
        log.info("warrant store opened at %s%s", self.path, " (read-only)" if read_only else "")

    def _migrate_v01(self) -> None:
        """0.1.0 stores chained per stream only; rebuild heads per (tenant, stream)."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(records)")}
        if not cols or "tenant" in cols:
            return
        log.info("warrant store: migrating %s to per-tenant chains", self.path)
        self._conn.executescript(
            """
            BEGIN;
            DROP TRIGGER IF EXISTS records_no_update;
            ALTER TABLE records ADD COLUMN tenant TEXT NOT NULL DEFAULT 'local';
            UPDATE records SET tenant = COALESCE(json_extract(body, '$.tenant'), 'local');
            DROP TABLE IF EXISTS stream_heads;
            CREATE TABLE stream_heads (tenant TEXT NOT NULL, stream TEXT NOT NULL, stream_seq INTEGER NOT NULL, hash TEXT NOT NULL, PRIMARY KEY (tenant, stream));
            INSERT INTO stream_heads (tenant, stream, stream_seq, hash)
                SELECT r.tenant, r.stream, r.stream_seq, r.hash FROM records r
                JOIN (SELECT tenant, stream, MAX(stream_seq) AS m FROM records GROUP BY tenant, stream) x
                  ON x.tenant = r.tenant AND x.stream = r.stream AND x.m = r.stream_seq;
            COMMIT;
            """
        )

    # -- Sink ----------------------------------------------------------------

    def write(self, records: Sequence[Dict[str, Any]]) -> None:
        """Seal and insert a batch in one transaction. Already-stored record_ids are skipped."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for record in records:
                    self._insert(record)
                self._conn.execute("COMMIT")
            except PermanentSinkError:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise sqlite3.OperationalError(f"warrant store write failed: {exc}") from exc

    def _insert(self, record: Dict[str, Any]) -> None:
        record_id = record.get("record_id")
        if not isinstance(record_id, str):
            raise PermanentSinkError("record has no record_id")
        if "seal" in record:
            raise PermanentSinkError(f"record {record_id} is already sealed; the store seals records itself")
        blobs = record.get("_blobs") or {}
        if blobs:
            record = {k: v for k, v in record.items() if k != "_blobs"}
            for digest, blob in blobs.items():
                self._conn.execute("INSERT OR IGNORE INTO evidence_blobs (hash, blob) VALUES (?, ?)", (digest, canonical_json(blob)))
        if self._conn.execute("SELECT 1 FROM records WHERE record_id = ?", (record_id,)).fetchone():
            log.info("warrant store skipping duplicate record %s", record_id)
            return
        stream = record.get("stream")
        if not isinstance(stream, str) or not stream:
            raise PermanentSinkError(f"record {record_id} has no stream")
        tenant = record.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise PermanentSinkError(f"record {record_id} has no tenant")
        head = self._conn.execute("SELECT stream_seq, hash FROM stream_heads WHERE tenant = ? AND stream = ?", (tenant, stream)).fetchone()
        stream_seq = (head["stream_seq"] + 1) if head else 1
        prev_hash = head["hash"] if head else None

        sealed = dict(record)
        sealed["sequence"] = stream_seq
        sealed["seal"] = {"prev_hash": prev_hash, "hash": record_hash(sealed, prev_hash)}
        try:
            validate(sealed)
        except ValidationError as exc:
            raise PermanentSinkError(f"record {record_id} failed schema validation: {exc.errors[0]}") from exc

        self._conn.execute(
            "INSERT INTO records (tenant, stream, stream_seq, record_id, record_type, subject, decision_record_id,"
            " timestamp, prev_hash, hash, body) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                stream,
                stream_seq,
                record_id,
                sealed["record_type"],
                (sealed.get("decision") or {}).get("subject"),
                (sealed.get("references") or {}).get("decision_record_id"),
                sealed["timestamp"],
                prev_hash,
                sealed["seal"]["hash"],
                canonical_json(sealed),
            ),
        )
        self._conn.execute(
            "INSERT INTO stream_heads (tenant, stream, stream_seq, hash) VALUES (?,?,?,?)"
            " ON CONFLICT(tenant, stream) DO UPDATE SET stream_seq = excluded.stream_seq, hash = excluded.hash",
            (tenant, stream, stream_seq, sealed["seal"]["hash"]),
        )

    # -- queries -------------------------------------------------------------

    def iter_records(self, stream: Optional[str] = None, tenant: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Yield sealed records in chain order, optionally for one stream and/or tenant."""
        sql = "SELECT body FROM records"
        clauses, params = [], []
        if stream is not None:
            clauses.append("stream = ?")
            params.append(stream)
        if tenant is not None:
            clauses.append("tenant = ?")
            params.append(tenant)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY tenant, stream, stream_seq"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        import json

        for row in rows:
            yield json.loads(row["body"])

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        import json

        with self._lock:
            row = self._conn.execute("SELECT body FROM records WHERE record_id = ?", (record_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def find_decision(self, stream: str, subject: str, tenant: Optional[str] = None) -> Optional[str]:
        """record_id of the most recent decision record for ``subject`` in ``stream`` (any tenant unless given)."""
        sql = "SELECT record_id FROM records WHERE stream = ? AND subject = ? AND record_type = 'decision'"
        params: list = [stream, subject]
        if tenant is not None:
            sql += " AND tenant = ?"
            params.append(tenant)
        sql += " ORDER BY seq DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return row["record_id"] if row else None

    def get_blob(self, content_hash: str) -> Optional[Dict[str, Any]]:
        """Captured evidence content for a hash, as the envelope written by ``encode_blob``."""
        import json

        with self._lock:
            row = self._conn.execute("SELECT blob FROM evidence_blobs WHERE hash = ?", (content_hash,)).fetchone()
        return json.loads(row["blob"]) if row else None

    def latest_outcome(self, decision_record_id: str) -> Optional[Dict[str, Any]]:
        """The most recent outcome record linked to a decision, if any."""
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM records WHERE decision_record_id = ? AND record_type = 'outcome' ORDER BY seq DESC LIMIT 1",
                (decision_record_id,),
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def streams(self) -> List[str]:
        with self._lock:
            return [r["stream"] for r in self._conn.execute("SELECT DISTINCT stream FROM stream_heads ORDER BY stream")]

    def count(self, stream: Optional[str] = None) -> int:
        with self._lock:
            if stream is None:
                return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return self._conn.execute("SELECT COUNT(*) FROM records WHERE stream = ?", (stream,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
