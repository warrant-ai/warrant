"""Asynchronous, batched record emission with a bounded buffer and disk spill.

The agent thread only enqueues. A daemon thread delivers batches to a sink. If the
sink fails, batches are spilled to JSONL files and retried with backoff once the sink
recovers; if the buffer is full, the caller spills directly. A sink that raises
``PermanentSinkError`` for a batch (for example a schema violation) has that batch
moved to a dead-letter directory instead of retried.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

log = logging.getLogger("warrant.emit")


class SinkError(Exception):
    """A transient delivery failure; the batch will be retried."""


class PermanentSinkError(SinkError):
    """A delivery failure that will not succeed on retry; the batch is dead-lettered."""


class Sink(Protocol):
    def write(self, records: Sequence[Dict[str, Any]]) -> None:
        """Persist a batch atomically, or raise."""


class Emitter:
    def __init__(
        self,
        sink: Sink,
        spill_dir: Path,
        *,
        max_queue: int = 10_000,
        batch_size: int = 200,
        flush_interval: float = 0.2,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
    ) -> None:
        if max_queue < 1 or batch_size < 1 or flush_interval <= 0:
            raise ValueError("max_queue and batch_size must be >= 1 and flush_interval > 0")
        self._sink = sink
        self._spill_dir = Path(spill_dir)
        self._dead_dir = self._spill_dir / "dead"
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._backoff = backoff_initial
        self._next_retry = 0.0
        self._healthy = True
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._pending = 0
        self._counters = {"submitted": 0, "delivered": 0, "spilled": 0, "recovered": 0, "failed_batches": 0, "dead": 0}
        self._spill_seq = itertools.count()
        self._thread = threading.Thread(target=self._run, name="warrant-emitter", daemon=True)
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._spill_dir.mkdir(parents=True, exist_ok=True)
        self._dead_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        self._thread.start()
        atexit.register(self.close)
        backlog = len(self._spill_files())
        if backlog:
            log.info("warrant emitter started with %d spill file(s) to recover", backlog)

    def submit(self, record: Dict[str, Any]) -> None:
        if not self._started:
            self.start()
        with self._cond:
            self._counters["submitted"] += 1
            self._pending += 1
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._spill([record], reason="buffer full")
            with self._cond:
                self._pending -= 1
                self._cond.notify_all()

    def flush(self, timeout: Optional[float] = 5.0) -> bool:
        """Block until every submitted record is delivered or safely spilled. Returns False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout=timeout)

    def close(self, timeout: Optional[float] = 5.0) -> None:
        if not self._started or self._stop.is_set():
            return
        self.flush(timeout)
        self._stop.set()
        self._thread.join(timeout)

    def stats(self) -> Dict[str, int]:
        with self._cond:
            return dict(self._counters, pending=self._pending, queued=self._queue.qsize())

    # -- worker --------------------------------------------------------------

    def _run(self) -> None:
        while True:
            batch = self._collect()
            if batch:
                self._deliver(batch)
            elif self._stop.is_set() and self._queue.empty():
                break
            self._drain_spill()

    def _collect(self) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        try:
            batch.append(self._queue.get(timeout=self._flush_interval))
        except queue.Empty:
            return batch
        deadline = time.monotonic() + self._flush_interval
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def _deliver(self, batch: List[Dict[str, Any]]) -> None:
        try:
            self._sink.write(batch)
            with self._cond:
                self._counters["delivered"] += len(batch)
            self._mark_healthy()
        except PermanentSinkError as exc:
            self._dead_letter(batch, exc)
        except Exception as exc:  # any sink failure is retried via spill
            self._mark_unhealthy(exc)
            self._spill(batch, reason="sink failure")
        finally:
            with self._cond:
                self._pending -= len(batch)
                self._cond.notify_all()

    def _drain_spill(self) -> None:
        if time.monotonic() < self._next_retry:
            return
        files = self._spill_files()
        if not files:
            return
        path = files[0]
        try:
            records = _read_jsonl(path)
        except (OSError, ValueError) as exc:
            log.error("warrant spill file %s unreadable, moving to dead letters: %s", path.name, exc)
            path.replace(self._dead_dir / path.name)
            return
        try:
            self._sink.write(records)
        except PermanentSinkError as exc:
            log.error("warrant spill file %s rejected permanently: %s", path.name, exc)
            path.replace(self._dead_dir / path.name)
            with self._cond:
                self._counters["dead"] += len(records)
            return
        except Exception as exc:
            self._mark_unhealthy(exc)
            return
        path.unlink()
        with self._cond:
            self._counters["recovered"] += len(records)
            self._counters["delivered"] += len(records)
        self._mark_healthy()

    # -- helpers -------------------------------------------------------------

    def _spill(self, records: Sequence[Dict[str, Any]], *, reason: str) -> None:
        self._spill_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns():020d}-{next(self._spill_seq):06d}.jsonl"
        path = self._spill_dir / name
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        with self._cond:
            self._counters["spilled"] += len(records)
        log.warning("warrant spilled %d record(s) to disk (%s)", len(records), reason)

    def _dead_letter(self, batch: Sequence[Dict[str, Any]], exc: Exception) -> None:
        name = f"{time.time_ns():020d}-{next(self._spill_seq):06d}.jsonl"
        with open(self._dead_dir / name, "w", encoding="utf-8") as fh:
            for record in batch:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self._cond:
            self._counters["dead"] += len(batch)
        log.error("warrant dead-lettered %d record(s) to %s: %s", len(batch), name, exc)

    def _mark_unhealthy(self, exc: Exception) -> None:
        with self._cond:
            self._counters["failed_batches"] += 1
        if self._healthy:
            log.error("warrant sink failing, retrying with backoff: %s", exc)
        self._healthy = False
        self._next_retry = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, self._backoff_max)

    def _mark_healthy(self) -> None:
        if not self._healthy:
            log.info("warrant sink recovered")
        self._healthy = True
        self._backoff = self._backoff_initial
        self._next_retry = 0.0

    def _spill_files(self) -> List[Path]:
        if not self._spill_dir.exists():
            return []
        return sorted(p for p in self._spill_dir.glob("*.jsonl") if p.is_file())


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
