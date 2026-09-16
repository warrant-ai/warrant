"""The gallery is the PRD's exit criterion: replay the demo set against a change and read the diff."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("celpy")

from warrant.cli import main  # noqa: E402
from warrant.replay import load_decider, load_targets, replay  # noqa: E402
from warrant.sets import DecisionSet  # noqa: E402

GALLERY = Path(__file__).resolve().parents[2] / "examples" / "gallery" / "lending"


@pytest.fixture
def in_gallery(monkeypatch):
    monkeypatch.chdir(GALLERY)
    monkeypatch.syspath_prepend(str(GALLERY))
    sys.modules.pop("underwriter", None)
    yield
    sys.modules.pop("underwriter", None)


def test_gallery_sets_and_targets_load(in_gallery):
    edge = DecisionSet.load(GALLERY / ".warrant" / "sets" / "lending-edge.jsonl")
    full = DecisionSet.load(GALLERY / ".warrant" / "sets" / "lending-all.jsonl")
    assert len(full) == 200 and 10 <= len(edge) <= 60
    assert all(i.outcome_label == "default" for i in edge)
    assert all(i.record["decision"].get("inputs") and i.evidence_content for i in full if i.record["decision"]["action"] != "decline")
    assert all(i.record["tenant"] == "gallery" and i.record["origin"] == "live" for i in full)
    targets = load_targets(GALLERY / ".warrant" / "targets.json")
    assert set(targets) == {"underwriter-v2.3", "underwriter-v2.4"} and targets["underwriter-v2.4"].params == {"threshold": 750}


def test_baseline_target_changes_nothing(in_gallery):
    targets = load_targets(".warrant/targets.json")
    edge = DecisionSet.load(".warrant/sets/lending-edge.jsonl")
    report = replay(edge, targets["underwriter-v2.3"], load_decider("underwriter:decide"), fail_on=["flipped", "new-deny", "unreplayable"])
    assert report.passed, report.failures()
    assert report.cost_change_pct == 0.0


def test_changed_target_flips_defaulted_loans_and_cuts_cost(in_gallery):
    targets = load_targets(".warrant/targets.json")
    edge = DecisionSet.load(".warrant/sets/lending-edge.jsonl")
    report = replay(edge, targets["underwriter-v2.4"], load_decider("underwriter:decide"), fail_on=["flipped"])
    assert not report.passed and len(report.flipped) >= 5
    assert report.flip_transitions() == {"approve -> refer": len(report.flipped)}
    assert report.flips_by_outcome() == {"default": len(report.flipped)}
    assert report.cost_change_pct is not None and report.cost_change_pct < -70


def test_gallery_cli_from_its_directory(in_gallery, capsys):
    assert main(["test", "lending-edge", "--against", "underwriter-v2.3", "--fail-on", "flipped"]) == 0
    assert "# 0 flipped" in capsys.readouterr().out
    assert main(["test", "lending-edge", "--against", "underwriter-v2.4", "--fail-on", "flipped"]) == 1
    out = capsys.readouterr().out
    assert "approve -> refer" in out and "by recorded outcome:" in out and "# FAILED:" in out
    assert main(["test", "lending-all", "--against", "underwriter-v2.4", "--max-cost-increase", "10%"]) == 0
    assert "# PASSED" in capsys.readouterr().out
