"""Generate conformance/admissibility-cases.json from the Python reference implementation.

Every case is an input to one of warrant.admissibility's pure functions and the output Python
produces. The JS SDK's admissibility module must reproduce each exactly; python/tests/
test_admissibility_cases.py re-derives the file so it can never drift from Python.

    python scripts/gen_admissibility_cases.py
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from warrant.admissibility import EDGES, STATES, assess, check_history, check_transition, legal

OUT = Path(__file__).resolve().parents[1] / "conformance" / "admissibility-cases.json"
AT = "2026-09-27T10:00:00.000Z"
FRESH = "2026-09-27T07:36:00.000Z"   # 0.1 day before AT
STALE = "2026-09-24T10:00:00.000Z"   # 3 days before AT
H = "a" * 64
P = "b" * 64
PARENT_ID = "01K5A3Q7Z2XV8M9N4B6C1D0001"
OB = {"id": "OB-1", "requires": "tool_call", "kind": "verifiable", "providers": ["cibil"], "max_age_seconds": 86400}


def item(**kw: Any) -> Dict[str, Any]:
    base = {"name": "bureau_pull", "type": "tool_call", "uri": "cibil://1", "content_hash": H,
            "provider": "cibil", "retrieved_at": FRESH, "obligation": "OB-1"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def record(*, evidence: List[Dict[str, Any]], obligations: List[Dict[str, Any]], result: str = "allow",
           status: str = "withheld", human: Dict[str, Any] = None, parents: List[Dict[str, Any]] = None,
           verdict: Dict[str, Any] = None, state_digest: str = None) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        "record_id": "01K5A3Q7Z2XV8M9N4B6C1D0099", "record_type": "decision", "timestamp": AT,
        "actor": {"name": "credit-agent", "version": "3"},
        "decision": {"class": "credit.approve", "action": "approve", "subject": "LN-1", "status": status},
        "mandate": {"result": result}, "evidence": evidence, "obligations": obligations,
        "human": human or {"required": False},
    }
    if parents is not None:
        r["parents"] = parents
    if verdict is not None:
        r["verdict"] = verdict
    if state_digest:
        r["decision"]["state_digest"] = state_digest
    return r


def assessed(rec: Dict[str, Any], at: Any) -> Dict[str, Any]:
    a = assess(rec, at=at)
    return {"obligations": a.obligations, "admissions": {str(k): v for k, v in a.admissions.items()},
            "met": a.met, "unmet": a.unmet, "advisory_unmet": a.advisory_unmet, "state": a.state, "history": a.history}


def main() -> None:
    ob_record = {**OB, "requires": "record", "providers": ["partner-data"]}
    parent_item = item(type="record", content_hash=P, provider="partner-data", parent={"record_id": PARENT_ID, "hash": P})
    human_ob = {"id": "OB-H", "requires": "human_review", "kind": "verifiable"}
    linked = {"required": True, "reviewer": "officer-7", "shown": [H]}

    assess_cases = [
        ("admitted and acted: committed", record(evidence=[item()], obligations=[OB], status="acted"), AT),
        ("rule 1: provider self", record(evidence=[item(provider="self")], obligations=[OB]), AT),
        ("rule 1: provider is the actor", record(evidence=[item(provider="credit-agent")], obligations=[OB]), AT),
        ("rule 1: no provider", record(evidence=[item(provider=None)], obligations=[OB]), AT),
        ("rule 2: unqualified provider", record(evidence=[item(provider="experian")], obligations=[OB]), AT),
        ("rule 3: stale", record(evidence=[item(retrieved_at=STALE)], obligations=[OB]), AT),
        ("rule 3: no timestamp", record(evidence=[item(retrieved_at=None)], obligations=[OB]), AT),
        ("rule 4: missing digest", record(evidence=[item(content_hash="")], obligations=[OB]), AT),
        ("rule 5: parent not cited", record(evidence=[parent_item], obligations=[ob_record]), AT),
        ("rule 5: parent cited but pending", record(evidence=[parent_item], obligations=[ob_record],
                                                    parents=[{"record_id": PARENT_ID, "hash": P, "state": "pending_evidence"}]), AT),
        ("rule 5: parent cited and committed", record(evidence=[parent_item], obligations=[ob_record],
                                                      parents=[{"record_id": PARENT_ID, "hash": P, "state": "committed"}]), AT),
        ("rule 5: parent cited with another hash", record(evidence=[parent_item], obligations=[ob_record],
                                                          parents=[{"record_id": PARENT_ID, "hash": H, "state": "committed"}]), AT),
        ("rule 6: wrong type", record(evidence=[item(type="document")], obligations=[OB]), AT),
        ("rule 6: wrong name", record(evidence=[item(name="other")], obligations=[{**OB, "name": "bureau_pull"}]), AT),
        ("rule order: self and stale reports self", record(evidence=[item(provider="self", retrieved_at=STALE)], obligations=[OB]), AT),
        ("rule 7: human linked satisfies without an item", record(evidence=[item(obligation=None)], obligations=[human_ob], human=linked), AT),
        ("rule 7: unnamed reviewer", record(evidence=[item(obligation=None)], obligations=[human_ob], human={"required": True, "shown": [H]}), AT),
        ("rule 7: shown digest not on the record", record(evidence=[item(obligation=None)], obligations=[human_ob],
                                                          human={"required": True, "reviewer": "r", "shown": ["c" * 64]}), AT),
        ("rule 7: shown digest is the state digest", record(evidence=[], obligations=[human_ob], state_digest="d" * 64,
                                                            human={"required": True, "reviewer": "r", "shown": ["d" * 64]}), AT),
        ("rule 7: a human_review item offered with a linked human", record(
            evidence=[item(type="human_review", name="review", provider="officer-desk", obligation="OB-H")],
            obligations=[human_ob], human=linked), AT),
        ("advisory unmet does not block", record(evidence=[item()], obligations=[OB, {"id": "OB-A", "requires": "document", "kind": "advisory"}]), AT),
        ("deny: refused", record(evidence=[item()], obligations=[OB], result="deny"), AT),
        ("escalate: escalated", record(evidence=[item()], obligations=[OB], result="escalate"), AT),
        ("unmet: pending_evidence", record(evidence=[item(provider="self")], obligations=[OB]), AT),
        ("failed after warrant: warranted", record(evidence=[item()], obligations=[OB], status="failed"), AT),
        ("no obligations and no verdict: no state", record(evidence=[item()], obligations=[]), AT),
        ("a verdict and no obligations, acted: committed", record(evidence=[], obligations=[], status="acted",
                                                                  verdict={"state": "proposed", "at": AT}), AT),
        ("two items for one obligation, one rejected", record(evidence=[item(provider="self"), item()], obligations=[OB]), AT),
        ("an item offered to an unknown obligation is ignored", record(evidence=[item(obligation="OB-9")], obligations=[OB]), AT),
        ("freshness judged at verdict.at when no time is given", record(evidence=[item(retrieved_at=STALE)], obligations=[OB],
                                                                        verdict={"state": "proposed", "at": "2026-09-24T12:00:00.000Z"}), None),
        ("two obligations in declared order", record(evidence=[item(), item(type="document", name="gst", provider="gstn", obligation="OB-2")],
                                                     obligations=[OB, {"id": "OB-2", "requires": "document", "kind": "verifiable", "providers": ["gstn"]}]), AT),
    ]

    decision = record(evidence=[item()], obligations=[OB, human_ob], verdict={"state": "pending_evidence", "unmet": ["OB-H"]})
    decision_evidence_unmet = record(evidence=[item()], obligations=[OB, human_ob], verdict={"state": "pending_evidence", "unmet": ["OB-1", "OB-H"]})

    def tr(state: str, from_state: Any = None, decided_by: Any = "human:officer-7", human: Dict[str, Any] = None) -> Dict[str, Any]:
        verdict = {"state": state}
        if from_state is not None:
            verdict["from_state"] = from_state
        if decided_by is not None:
            verdict["decided_by"] = decided_by
        t: Dict[str, Any] = {"record_type": "transition", "verdict": verdict}
        if human is not None:
            t["human"] = human
        return t

    transition_cases = [
        ("warranted to committed", decision, "warranted", tr("committed", "warranted", "agent:sanction")),
        ("pending to refused", decision, "pending_evidence", tr("refused", "pending_evidence")),
        ("committed only from warranted", decision, "pending_evidence", tr("committed")),
        ("from_state must match", decision, "warranted", tr("committed", "escalated")),
        ("escalated to warranted needs a linked human", decision, "escalated", tr("warranted", "escalated")),
        ("escalated to warranted with a linked human", decision, "escalated", tr("warranted", "escalated", human=linked)),
        ("escalated to warranted with an unnamed human", decision, "escalated", tr("warranted", "escalated", human={"shown": [H]})),
        ("pending to warranted cannot add evidence", decision_evidence_unmet, "pending_evidence", tr("warranted", human=linked)),
        ("pending to warranted with only a human unmet and linked", decision, "pending_evidence", tr("warranted", human=linked)),
        ("pending to warranted with only a human unmet, not linked", decision, "pending_evidence", tr("warranted")),
        ("a transition must say who decided", decision, "warranted", tr("committed", decided_by=None)),
        ("terminal states go nowhere", decision, "refused", tr("warranted")),
        ("an unknown target state", decision, "proposed", tr("archived")),
    ]

    history_cases = [
        ("empty", []),
        ("legal path", ["proposed", "warranted", "committed"]),
        ("legal via escalation", ["proposed", "escalated", "warranted", "committed"]),
        ("wrong start", ["warranted", "committed"]),
        ("committed from pending", ["proposed", "pending_evidence", "committed"]),
        ("out of refused", ["proposed", "refused", "warranted"]),
    ]

    out = {
        "spec": "adr/0.2",
        "note": "Generated by scripts/gen_admissibility_cases.py from warrant.admissibility. Do not edit by hand.",
        "states": list(STATES),
        "edges": {k: list(v) for k, v in EDGES.items()},
        "legal": [[a, b, legal(a, b)] for a in STATES + ("unknown",) for b in STATES],
        "assess": [{"name": n, "record": r, "at": at, "expected": assessed(r, at)} for n, r, at in assess_cases],
        "history": [{"name": n, "history": h, "expected": check_history(h)} for n, h in history_cases],
        "transition": [{"name": n, "decision": d, "current": c, "transition": t, "expected": check_transition(d, c, t)}
                       for n, d, c, t in transition_cases],
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(out['assess'])} assess, {len(out['history'])} history, {len(out['transition'])} transition cases")


if __name__ == "__main__":
    main()
