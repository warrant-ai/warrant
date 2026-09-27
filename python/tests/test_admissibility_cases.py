"""conformance/admissibility-cases.json is shared with the JS SDK: it must match Python exactly."""

import json
from pathlib import Path

from warrant.admissibility import EDGES, STATES, assess, check_history, check_transition, legal

CASES = json.loads((Path(__file__).resolve().parents[2] / "conformance" / "admissibility-cases.json").read_text())


def test_the_states_and_edges_match():
    assert CASES["states"] == list(STATES)
    assert CASES["edges"] == {k: list(v) for k, v in EDGES.items()}
    for a, b, expected in CASES["legal"]:
        assert legal(a, b) is expected, (a, b)


def test_every_assess_case_matches():
    for case in CASES["assess"]:
        a = assess(case["record"], at=case["at"])
        got = {"obligations": a.obligations, "admissions": {str(k): v for k, v in a.admissions.items()},
               "met": a.met, "unmet": a.unmet, "advisory_unmet": a.advisory_unmet, "state": a.state, "history": a.history}
        assert got == case["expected"], case["name"]


def test_every_history_and_transition_case_matches():
    for case in CASES["history"]:
        assert check_history(case["history"]) == case["expected"], case["name"]
    for case in CASES["transition"]:
        assert check_transition(case["decision"], case["current"], case["transition"]) == case["expected"], case["name"]
