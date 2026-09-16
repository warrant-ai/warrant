"""Local append-only store (SQLite) that seals records into a per-stream hash chain.

Records arrive unsealed. The store assigns the next ``sequence`` in the stream, sets
``seal.prev_hash`` to the stream head, computes ``seal.hash`` over the body, validates
the sealed record against the schema, and inserts it. Triggers refuse UPDATE and
DELETE so the file itself is append-only.
"""

from __future__ import annotations

import logging
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
    UNIQUE (stream, stream_seq)
);
CREATE INDEX IF NOT EXISTS records_subject ON records (stream, subject, record_type);
CREATE INDEX IF NOT EXISTS records_decision ON records (decision_record_id);
CREATE TABLE IF NOT EXISTS evidence_blobs (
    hash TEXT PRIMARY KEY,
    blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_heads (
    stream TEXT PRIMARY KEY,
    stream_seq INTEGER NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'warrant records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'warrant records are append-only'); END;
"""


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
            self._conn.executescript(_DDL)
        self._conn.row_factory = sqlite3.Row
        log.info("warrant store opened at %s%s", self.path, " (read-only)" if read_only else "")

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
        head = self._conn.execute("SELECT stream_seq, hash FROM stream_heads WHERE stream = ?", (stream,)).fetchone()
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
            "INSERT INTO records (stream, stream_seq, record_id, record_type, subject, decision_record_id,"
            " timestamp, prev_hash, hash, body) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
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
            "INSERT INTO stream_heads (stream, stream_seq, hash) VALUES (?,?,?)"
            " ON CONFLICT(stream) DO UPDATE SET stream_seq = excluded.stream_seq, hash = excluded.hash",
            (stream, stream_seq, sealed["seal"]["hash"]),
        )

    # -- queries -------------------------------------------------------------

    def iter_records(self, stream: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Yield sealed records in chain order, optionally for one stream."""
        sql = "SELECT body FROM records"
        params: tuple = ()
        if stream is not None:
            sql += " WHERE stream = ?"
            params = (stream,)
        sql += " ORDER BY stream, stream_seq"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        import json

        for row in rows:
            yield json.loads(row["body"])

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        import json

        with self._lock:
            row = self._conn.execute("SELECT body FROM records WHERE record_id = ?", (record_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def find_decision(self, stream: str, subject: str) -> Optional[str]:
        """record_id of the most recent decision record for ``subject`` in ``stream``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT record_id FROM records WHERE stream = ? AND subject = ? AND record_type = 'decision'"
                " ORDER BY stream_seq DESC LIMIT 1",
                (stream, subject),
            ).fetchone()
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
                "SELECT body FROM records WHERE decision_record_id = ? AND record_type = 'outcome' ORDER BY stream_seq DESC LIMIT 1",
                (decision_record_id,),
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def streams(self) -> List[str]:
        with self._lock:
            return [r["stream"] for r in self._conn.execute("SELECT stream FROM stream_heads ORDER BY stream")]

    def count(self, stream: Optional[str] = None) -> int:
        with self._lock:
            if stream is None:
                return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return self._conn.execute("SELECT COUNT(*) FROM records WHERE stream = ?", (stream,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
