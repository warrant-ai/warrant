"""Rebuild the gallery sets. Run from this directory: python build.py

Records 200 synthetic loan decisions with inputs and evidence captured, attaches
outcomes with a deliberate pattern (approvals in the 720-749 band default more
often), and saves two sets: lending-all and lending-edge (the defaults).
"""

import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import underwriter  # noqa: E402

from warrant import AgentInfo, SQLiteStore, Warrant  # noqa: E402
from warrant.replay import Target, save_targets  # noqa: E402
from warrant.sets import build_set  # noqa: E402

HERE = Path(__file__).parent
POLICIES = HERE.parent.parent / "policies"


def main() -> None:
    rng = random.Random(2026)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "records.db"
        with Warrant("lending", tenant="gallery", store=db, agent=AgentInfo("credit-underwriter", "2.3.1"),
                     policy_bundle=POLICIES, currency="INR", capture_inputs=True, capture_evidence=True, flush_interval=0.02) as w:
            for i in range(200):
                subject = f"LN-{40000 + i}"
                inputs = {"amount": rng.choice([150000, 300000, 450000, 480000, 700000]),
                          "bureau_score": rng.randint(600, 820), "foir": round(rng.uniform(0.2, 0.6), 2)}
                with w.decide("credit.approve", subject=subject, on_behalf_of="branch:jayanagar") as d:
                    underwriter.decide(d, inputs)
                if not d.acted:
                    continue
                score = inputs["bureau_score"]
                if 720 <= score < 750 and rng.random() < 0.6:
                    w.outcome(subject=subject, label="default", observed_at="2026-12-15T00:00:00Z", source="lms://gallery")
                elif rng.random() < 0.5:
                    w.outcome(subject=subject, label="performing", observed_at="2026-12-15T00:00:00Z", source="lms://gallery")
            assert w.flush()
            store = SQLiteStore(db, read_only=True)
            for name, where in (("lending-all", None), ("lending-edge", "outcome.label == 'default'")):
                ds = build_set(store, name, stream="lending", where=where)
                ds.source = {"gallery": "lending", "where": where, "synthetic": True}
                ds.save(HERE / ".warrant" / "sets" / f"{name}.jsonl")
                print(f"{name}: {len(ds)} decisions")
            store.close()
    save_targets(HERE / ".warrant" / "targets.json", {
        "underwriter-v2.3": Target("underwriter-v2.3", AgentInfo("credit-underwriter", "2.3.1"), model="anthropic/claude-sonnet-5", policy="../../policies", decider="underwriter:decide"),
        "underwriter-v2.4": Target("underwriter-v2.4", AgentInfo("credit-underwriter", "2.4.0"), model="anthropic/claude-haiku-4-5-20251001", policy="../../policies", decider="underwriter:decide", params={"threshold": 750}),
    })
    print("targets.json written")


if __name__ == "__main__":
    main()
