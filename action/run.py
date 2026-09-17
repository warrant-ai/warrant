"""Entry point of the Warrant replay action: run `warrant test`, summarise, set outputs, keep its exit code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

MAX_ROWS = 25


def summary_markdown(report: Dict[str, Any]) -> str:
    """The job summary: the verdict, the numbers a reviewer needs, and the decisions that changed."""
    totals, gates = report["totals"], report["gates"]
    lines = [f"## Warrant replay: {'passed' if gates['passed'] else 'failed'}", "",
             f"`{report['set']}` against `{report['target']}` ({report['mode']}), {totals['replayed']} real decisions replayed.", ""]
    for failure in gates["failures"]:
        lines.append(f"- **{_cell(failure)}**")
    if gates["failures"]:
        lines.append("")
    lines += ["| | |", "|---|---|",
              f"| Decisions flipped | {totals['flipped']} |",
              f"| Newly denied by policy | {totals['new_deny']} |",
              f"| Newly escalated | {totals['new_escalate']} |",
              f"| Unreplayable | {totals['unreplayable']} |",
              f"| Errored | {totals['errored']} |",
              f"| Cost per decision | {_per(totals['cost_before'], totals['replayed'])} → {_per(totals['cost_after'], totals['replayed'])} ({totals['cost_change_pct']:+.0f}%) |"]
    by_outcome = report.get("flips_by_outcome") or {}
    if by_outcome:
        lines.append("| Flips by recorded outcome | " + ", ".join(f"{n} {_cell(label)}" for label, n in by_outcome.items()) + " |")
    changed: List[Dict[str, Any]] = [r for r in report["results"] if r["flipped"] or r["new_deny"] or r["status"] != "replayed"]
    if changed:
        lines += ["", "| Subject | Recorded | Replayed | Mandate | Recorded outcome | Note |", "|---|---|---|---|---|---|"]
        for r in changed[:MAX_ROWS]:
            mandate = r["after_mandate"] if r["before_mandate"] == r["after_mandate"] else f"{r['before_mandate']} → {r['after_mandate']}"
            note = r["detail"] if r["status"] != "replayed" else ""
            lines.append(f"| {_cell(r['subject'])} | {_cell(r['before_action'])} | {_cell(r['after_action'])} | {_cell(mandate)} | {_cell(r['outcome_label'] or '')} | {_cell(note)} |")
        if len(changed) > MAX_ROWS:
            lines.append(f"\n{len(changed) - MAX_ROWS} more in the JSON report.")
    return "\n".join(lines) + "\n"


def _per(total: float, count: int) -> str:
    return f"{total / count:.2f}" if count else "0.00"


def _cell(value: Any) -> str:
    """Record text is data from someone's production system: keep it inside its table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")[:200]


def main() -> int:
    env = os.environ
    out_dir = Path(env.get("RUNNER_TEMP") or tempfile.mkdtemp())
    report_path, junit_path = out_dir / "warrant-replay.json", out_dir / "warrant-replay.xml"
    command = ["warrant", "--workspace", env.get("WARRANT_WORKSPACE") or ".warrant", "test", env["WARRANT_SET"], "--against", env["WARRANT_AGAINST"],
               "--mode", env.get("WARRANT_MODE") or "frozen", "--json", str(report_path), "--junit", str(junit_path)]
    if env.get("WARRANT_FAIL_ON"):
        command += ["--fail-on", env["WARRANT_FAIL_ON"]]
    if env.get("WARRANT_MAX_COST"):
        command += ["--max-cost-increase", env["WARRANT_MAX_COST"]]
    code = subprocess.run(command, check=False).returncode
    if not report_path.exists():
        print("::error::warrant test did not produce a report; see the log above", flush=True)
        return code or 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(report))
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"passed={'true' if report['gates']['passed'] else 'false'}\nflipped={report['totals']['flipped']}\nreport={report_path}\n")
    for failure in report["gates"]["failures"]:
        print(f"::error title=Warrant replay::{failure}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
