"""The decider for the AML gallery: six typed questions, one disposition, one confidence.

Synthetic throughout. It stands in for whatever answers the questions in a real deployment — a
model, an ensemble, a scorecard — and exists so the recording, policy, outcome and calibration
path can be exercised end to end on a clean machine without a vendor or an API key.

The segment carve-outs are the part worth copying. `trade_finance` and `pep` never route to AUTO
at any confidence, because the failure mode that matters is not a missed fraud but an alert closed
by machine that should have become a suspicious transaction report.
"""

from typing import Any, Dict, Tuple

QUESTION_SET = {"id": "aml.alert", "version": "3.1.0"}
#: The questions themselves live in question-sets/aml.alert@3.1.0.yaml and are stamped on every
#: record; `warrant questions lint` guards the version whenever one of them is edited.

SEGMENTS = ("retail", "retail", "retail", "sme", "sme", "trade_finance", "pep")
NEVER_AUTO = ("trade_finance", "pep")


def sample_alert(rng) -> Dict[str, Any]:
    """One synthetic alert's derived state — ratios, bands and flags, never identifiers.

    The risk features are drawn from a distribution skewed low, because transaction monitoring
    is 95-98% false positives: most alerts really are unremarkable, and a population drawn
    uniformly would make the whole exercise look harder than it is.
    """
    return {
        "segment": rng.choice(SEGMENTS),
        "profile_consistent": rng.random() < 0.72,
        "structuring_pattern": rng.random() < 0.12,
        "counterparty_risk": round(rng.betavariate(1.5, 6.0), 2),
        "explanation_on_file": rng.random() < 0.58,
        "behaviour_change": round(rng.betavariate(1.5, 6.0), 2),
        "amount_band": rng.choice(["under_1l", "1l_5l", "5l_20l", "over_20l"]),
    }


def _confidence(alert: Dict[str, Any]) -> float:
    """How sure the adjudicator is that this alert can be closed."""
    score = 0.55
    score += 0.30 if alert["profile_consistent"] else -0.22
    score += 0.18 if alert["explanation_on_file"] else -0.08
    score -= 0.35 if alert["structuring_pattern"] else 0.0
    score -= 0.22 * alert["counterparty_risk"]
    score -= 0.18 * alert["behaviour_change"]
    return round(min(0.98, max(0.05, score)), 2)


def decide(d, alert: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """Answer the question set, check the mandate, and act. Returns the answers and the verdict."""
    confidence = _confidence(alert)
    close = confidence >= 0.5 and not alert["structuring_pattern"]
    answers = {
        "profile_consistent": {"value": alert["profile_consistent"], "confidence": 0.86},
        "structuring_pattern": {"value": alert["structuring_pattern"], "confidence": 0.81},
        "counterparty_risk": {"value": alert["counterparty_risk"], "confidence": 0.74},
        "explanation_on_file": {"value": alert["explanation_on_file"], "confidence": 0.90},
        "behaviour_change": {"value": alert["behaviour_change"], "confidence": 0.69},
        "disposition": {
            "value": "close" if close else "escalate",
            "confidence": confidence,
            "distribution": {
                "close" if close else "escalate": confidence,
                "escalate" if close else "close": round(1.0 - confidence, 2),
            },
        },
    }

    d.question_set(QUESTION_SET["id"], QUESTION_SET["version"])
    for name, body in answers.items():
        # The distribution, not only the winning value: the runners-up are what calibration needs.
        # Every name here is a question in aml.alert@3.1.0 and nothing else — a record that answers
        # a question its stated version does not contain is the drift the registry exists to stop.
        d.answer(
            name,
            body["value"],
            confidence=body["confidence"],
            distribution=body.get("distribution"),
        )

    verdict = d.check(
        segment=alert["segment"],
        disposition=answers["disposition"]["value"],
        confidence=confidence,
        structuring_pattern=alert["structuring_pattern"],
    )
    d.model_call("gallery", "adjudicator-sim", tokens_in=1800, tokens_out=40, amount=0.0)

    acted = verdict.allowed and close
    d.act(
        "close" if acted else "refer",
        cost_centre="fiu-ops",
        alternatives=["close", "refer"],
        route="auto" if acted else "human",
    )
    return answers, verdict
