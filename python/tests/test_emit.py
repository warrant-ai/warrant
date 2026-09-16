import logging
import threading
import time

import pytest

from warrant.emit import Emitter, PermanentSinkError


class FlakySink:
    def __init__(self):
        self.written = []
        self.fail = False
        self.lock = threading.Lock()

    def write(self, records):
        if self.fail:
            raise ConnectionError("store down")
        with self.lock:
            self.written.extend(records)


def _rec(i):
    return {"record_id": f"r{i}", "stream": "s"}


def test_delivers_in_order_and_flush_waits(tmp_path):
    sink = FlakySink()
    em = Emitter(sink, tmp_path / "spill", flush_interval=0.02)
    em.start()
    for i in range(500):
        em.submit(_rec(i))
    assert em.flush(timeout=5)
    assert [r["record_id"] for r in sink.written] == [f"r{i}" for i in range(500)]
    assert em.stats()["delivered"] == 500
    em.close()


def test_sink_outage_spills_and_recovers(tmp_path, caplog):
    sink = FlakySink()
    sink.fail = True
    em = Emitter(sink, tmp_path / "spill", flush_interval=0.02, backoff_initial=0.05, backoff_max=0.1)
    em.start()
    with caplog.at_level(logging.ERROR, logger="warrant.emit"):
        for i in range(10):
            em.submit(_rec(i))
        assert em.flush(timeout=5)
    assert sink.written == []
    spilled = list((tmp_path / "spill").glob("*.jsonl"))
    assert spilled and em.stats()["spilled"] == 10
    assert any("sink failing" in r.message for r in caplog.records)

    sink.fail = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(sink.written) < 10:
        time.sleep(0.05)
    assert sorted(r["record_id"] for r in sink.written) == sorted(f"r{i}" for i in range(10))
    assert not list((tmp_path / "spill").glob("*.jsonl"))
    assert em.stats()["recovered"] == 10
    em.close()


def test_spill_survives_restart(tmp_path):
    sink = FlakySink()
    sink.fail = True
    em = Emitter(sink, tmp_path / "spill", flush_interval=0.02, backoff_initial=0.05)
    em.start()
    em.submit(_rec(1))
    assert em.flush(timeout=5)
    em.close()
    assert list((tmp_path / "spill").glob("*.jsonl"))

    sink2 = FlakySink()
    em2 = Emitter(sink2, tmp_path / "spill", flush_interval=0.02)
    em2.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not sink2.written:
        time.sleep(0.05)
    assert sink2.written == [_rec(1)]
    em2.close()


def test_full_buffer_spills_without_blocking(tmp_path):
    class Blocking:
        def __init__(self):
            self.gate = threading.Event()
            self.written = []

        def write(self, records):
            self.gate.wait(5)
            self.written.extend(records)

    sink = Blocking()
    em = Emitter(sink, tmp_path / "spill", max_queue=5, batch_size=1, flush_interval=0.01)
    em.start()
    start = time.monotonic()
    for i in range(50):
        em.submit(_rec(i))
    assert time.monotonic() - start < 1.0
    assert em.stats()["spilled"] > 0
    sink.gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(sink.written) < 50:
        time.sleep(0.05)
    assert sorted(r["record_id"] for r in sink.written) == sorted(f"r{i}" for i in range(50))
    em.close()


def test_permanent_failure_is_dead_lettered_not_retried(tmp_path, caplog):
    class Rejecting:
        def write(self, records):
            raise PermanentSinkError("schema")

    em = Emitter(Rejecting(), tmp_path / "spill", flush_interval=0.02)
    em.start()
    with caplog.at_level(logging.ERROR, logger="warrant.emit"):
        em.submit(_rec(1))
        assert em.flush(timeout=5)
    assert em.stats()["dead"] == 1
    assert list((tmp_path / "spill" / "dead").glob("*.jsonl"))
    assert not list((tmp_path / "spill").glob("*.jsonl"))
    em.close()


def test_rejects_bad_configuration(tmp_path):
    with pytest.raises(ValueError):
        Emitter(FlakySink(), tmp_path, max_queue=0)
