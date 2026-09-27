"""Smoke test for the handoff benchmark (bench/handoff): it runs, reports every measure, and arm 3
attributes every injected evidence fault it can see."""

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_a_small_benchmark_reports_every_measure(tmp_path):
    from bench.handoff.run import ARMS, render, run_benchmark

    started = time.perf_counter()
    results = run_benchmark(cases=12, seed=3, sign_samples=200, write_samples=20, workdir=tmp_path)
    assert time.perf_counter() - started < 20
    for key in ("blame", "e2e", "completion_gap", "overhead", "latency", "upstream_origin"):
        assert key in results
    assert results["blame"]["overall"][ARMS[0]]["agent"] == "not run"
    assert results["latency"]["sign_seal_ms"]["p99"] > 0
    assert set(results["completion_gap"]) == {"3", "4", "5"}
    adr = ARMS[3]
    seen = 0
    for kind in ("bad_source", "false_completion", "skipped_check"):
        per_arm = results["blame"]["fault_kind"].get(kind)
        if per_arm and per_arm[adr]["n"]:
            seen += 1
            assert per_arm[adr]["step"] == 1.0, (kind, per_arm[adr])
    assert seen, "the seed should inject at least one evidence fault"
    assert results["blame"]["overall"][adr]["false_alarms"] == 0.0
    report = render(results)
    assert "Predictions from the thesis" in report and "not testable" in report.lower()
