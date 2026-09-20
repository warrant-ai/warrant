"""Rebuild the AML gallery. Run from this directory: python build.py

Records 500 synthetic transaction-monitoring alert dispositions, each answered by a six-question
set with a stated confidence, checks them against AML-01, then attaches the 90-day outcome for the
four in five where the observation window has closed.

The adjudicator here is deliberately **overconfident in its top band**: alerts it closes at 0.95
stay closed less often than that. This is the failure mode the product exists to catch, and it is
the one a bank must see before it automates anything. Nothing in this directory is real or derived
from a real institution.
"""

import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import adjudicator  # noqa: E402

from warrant import AgentInfo, SQLiteStore, Warrant  # noqa: E402
from warrant.outcomes import ingest_outcomes  # noqa: E402

HERE = Path(__file__).parent
POLICIES = HERE / "policies"
ALERTS = 500
OUTCOME_HORIZON_DAYS = 90
# Four in five alerts are old enough for the 90-day window to have closed. The rest are pending,
# because a pack that pretends every decision has an outcome is not an honest one.
OBSERVED_SHARE = 0.8


def main() -> None:
    rng = random.Random(2026)
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    store_path = HERE / "records.db"
    if store_path.exists():
        store_path.unlink()

    rows = ["subject,label,source"]
    with Warrant(
        "aml",
        tenant="gallery",
        store=store_path,
        agent=AgentInfo("alert-adjudicator", "1.4.0"),
        policy_bundle=POLICIES,
        currency="INR",
        capture_inputs=True,
        flush_interval=0.02,
    ) as w:
        for i in range(ALERTS):
            subject = f"alert:TM-{70000 + i}"
            raised = start + timedelta(hours=i * 3)
            alert = adjudicator.sample_alert(rng)
            with w.decide("aml.alert.disposition", subject=subject) as d:
                answers, verdict = adjudicator.decide(d, alert)
            if not d.acted:
                continue
            if rng.random() > OBSERVED_SHARE:
                continue  # the 90-day window has not closed yet
            stated = answers["disposition"]["confidence"]
            # The adjudicator is well calibrated below 0.9 and overstates itself above it. That
            # is the whole point of the gallery: the band a bank would automate first is the one
            # where the stated confidence is furthest from the truth, and nothing but an
            # outcome-linked ledger shows it.
            true_rate = stated if stated < 0.9 else stated - 0.12
            stayed_closed = rng.random() < true_rate
            label = "stayed_closed" if stayed_closed else "reopened"
            # No synthetic observed_at: the decisions are stamped when this script runs, so any
            # backdated observation would land before the decision it describes. The 90-day
            # horizon is what this set represents, not something faked into the timestamps.
            rows.append(f"{subject},{label},case-system")

    outcomes_csv = HERE / "outcomes.csv"
    outcomes_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    store = SQLiteStore(store_path)
    report = ingest_outcomes([outcomes_csv], store, stream="aml")
    store.close()
    print(report.summary())
    print(f"\nstore: {store_path}\noutcomes: {outcomes_csv}")
    print("\nnext:")
    print(f"  warrant calibrate --store {store_path} --stream aml \\")
    print('    --correct-when "outcome.label == \'stayed_closed\'" --by inputs.segment')
    print(f"  warrant pack --store {store_path} --stream aml -o /tmp/aml-pack \\")
    print(f'    --policy {POLICIES} --correct-when "outcome.label == \'stayed_closed\'" --by inputs.segment')


if __name__ == "__main__":
    main()
