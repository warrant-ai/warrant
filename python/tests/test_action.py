"""The GitHub Action's summary is built from replay reports, which carry text from production records."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("warrant_action_run", Path(__file__).parent.parent.parent / "action" / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def report(results, *, passed, failures=(), flipped=0):
    return {"set": "lending-edge", "target": "v2.4", "mode": "frozen",
            "totals": {"replayed": len(results), "flipped": flipped, "new_deny": 0, "new_escalate": 0, "unreplayable": 0, "errored": 0,
                       "cost_before": 3.84 * len(results), "cost_after": 0.9 * len(results), "cost_change_pct": -76.6},
            "flips_by_outcome": {"default": flipped} if flipped else {}, "gates": {"passed": passed, "failures": list(failures)}, "results": results}


def row(subject, *, flipped=False, status="replayed", detail="", outcome=None):
    return {"subject": subject, "before_action": "approve", "after_action": "refer" if flipped else "approve", "before_mandate": "allow",
            "after_mandate": "allow", "outcome_label": outcome, "status": status, "detail": detail, "flipped": flipped, "new_deny": False}


def test_a_passing_replay_is_a_short_summary():
    text = run.summary_markdown(report([row("LN-1"), row("LN-2")], passed=True))
    assert text.startswith("## Warrant replay: passed\n")
    assert "2 real decisions replayed" in text and "| Cost per decision | 3.84 → 0.90 (-77%) |" in text
    assert "| Subject |" not in text and "Flips by recorded outcome" not in text


def test_a_failing_replay_names_the_gate_and_the_decisions():
    results = [row("LN-1"), row("LN-2", flipped=True, outcome="default"), row("LN-3", status="unreplayable", detail="tool bureau was never recorded")]
    text = run.summary_markdown(report(results, passed=False, failures=["1 flipped decision(s) exceed threshold 0"], flipped=1))
    assert "## Warrant replay: failed" in text and "- **1 flipped decision(s) exceed threshold 0**" in text
    assert "| LN-2 | approve | refer | allow | default |  |" in text
    assert "| LN-3 | approve | approve | allow |  | tool bureau was never recorded |" in text
    assert "| LN-1 |" not in text and "| Flips by recorded outcome | 1 default |" in text


def test_record_text_cannot_break_out_of_its_cell_and_long_lists_are_capped():
    hostile = row("LN-9 | <img src=x onerror=alert(1)>\n## Approved", flipped=True)
    text = run.summary_markdown(report([hostile] + [row(f"LN-{i}", flipped=True) for i in range(40)], passed=False, flipped=41))
    assert "LN-9 \\| &lt;img src=x onerror=alert(1)> ## Approved" in text and "\n## Approved" not in text
    assert text.count("| approve | refer |") == run.MAX_ROWS and "16 more in the JSON report." in text


def test_an_empty_set_does_not_divide_by_zero():
    assert "| Cost per decision | 0.00 → 0.00 (-77%) |" in run.summary_markdown(report([], passed=True))


def test_a_baseline_with_no_recorded_cost_has_no_percentage_to_show():
    free = report([row("LN-1")], passed=True)
    free["totals"].update(cost_before=0.0, cost_after=0.9, cost_change_pct=None)
    assert "| Cost per decision | 0.00 → 0.90 |" in run.summary_markdown(free)
