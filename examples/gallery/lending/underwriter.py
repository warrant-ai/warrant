"""Gallery underwriter: the agent whose recorded decisions the gallery set holds.

The decision it makes is deliberately simple so the replay diff is easy to read:
approve when the mandate allows it and the bureau score clears THRESHOLD, refer
otherwise. A target can override the threshold and the model through its params,
which is the gallery's stand-in for "change one line and see what flips".
"""

THRESHOLD = 720
MODEL = "anthropic/claude-sonnet-5"
PRICE = {"anthropic/claude-sonnet-5": 3.84, "anthropic/claude-haiku-4-5-20251001": 0.90}


def pull_bureau(subject: str, score: int) -> dict:
    """Stands in for a credit bureau call. Deterministic so the gallery is reproducible."""
    return {"subject": subject, "score": score, "enquiries": score % 5}


def decide(d, inputs, target=None):
    params = target.params if target is not None else {}
    threshold = int(params.get("threshold", THRESHOLD))
    model = params.get("model") or (target.model if target is not None and target.model else MODEL)

    verdict = d.check(amount=inputs["amount"], bureau_score=inputs["bureau_score"], foir=inputs["foir"])
    if verdict.result == "deny":
        d.act("decline", summary=verdict.reason)
        return

    bureau = d.tool("bureau_pull", lambda: pull_bureau(d.subject, inputs["bureau_score"]), uri=f"cibil://req/{d.subject}")
    provider, name = model.split("/", 1)
    d.model_call(provider, name, tokens_in=6000, tokens_out=400, amount=PRICE.get(model, 1.0))

    if verdict.result == "escalate":
        d.act("refer", summary="referred to a credit officer under mandate")
    elif bureau["score"] >= threshold and inputs["foir"] <= 0.45:
        d.act("approve", summary=f"auto-approved at bureau score {bureau['score']}")
    else:
        d.act("refer", summary=f"bureau score {bureau['score']} below {threshold}")
