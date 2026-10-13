"""Run the whole platform end to end, on one machine, in about ten seconds.

    python run_platform.py

Starts a Temporal test server with time skipping, a worker, and a run of synthetic alerts through
the three layers. Because the server skips time, the ninety-day outcome horizon really elapses —
the workflows wait it out, wake, ask the system of record what happened, and link the outcome. That
is the part of the loop nothing else in the stack can do, and it is the part worth watching.

Then it does what a design partner would be shown on day fourteen: the reliability curve over the
band that was automated, and an evidence pack that verifies with nothing but the CLI.

With ``TYPESAFE_API_KEY`` set it uses Jev, pinned to ``JEV_MODEL`` (default ``jev-1.13.0``).
Without one it uses a scripted model so the demo still runs; the output says which, because a
reliability curve from a scripted model proves nothing and should never be mistaken for one that does.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import shutil
import sys
from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from warrant import AgentInfo, Warrant
from warrant.store import open_store
from warrant.adapters.base import DecisionModel, ModelAnswer, ModelResult
from warrant.adapters.model import DecisionAdapter
from warrant.adapters.temporal import WarrantInterceptor

sys.path.insert(0, str(Path(__file__).parent))
import alert_workflow as app  # noqa: E402

HERE = Path(__file__).parent
POLICIES = HERE.parent / "gallery" / "aml" / "policies"
QUESTION_SETS = HERE.parent / "gallery" / "aml" / "question-sets"
QUESTION_SET = ("aml.alert", "3.1.0")
SEGMENTS = ("retail", "retail", "retail", "sme", "sme", "trade_finance", "pep")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")


class ScriptedModel(DecisionModel):
    """Stands in for a decision model when no vendor key is configured.

    Deliberately overconfident above 0.90, because a demo where the model is perfect shows nothing:
    the finding worth seeing is that the band you were about to automate is worse than it claims.
    """

    provider = "scripted"
    endpoint = "https://scripted.invalid"
    region = "global"

    def __init__(self, seed: int = 2026) -> None:
        self._rng = random.Random(seed)

    def evaluate(self, state, questions):
        score = 0.55
        score += 0.30 if state["profile_consistent"] else -0.22
        score += 0.18 if state["explanation_on_file"] else -0.08
        score -= 0.35 if state["structuring_pattern"] else 0.0
        score -= 0.22 * state["counterparty_risk"]
        score -= 0.18 * state["behaviour_change"]
        confidence = round(min(0.98, max(0.05, score)), 2)
        close = confidence >= 0.5 and not state["structuring_pattern"]
        value = "close" if close else "escalate"
        # Every question in aml.alert@3.1.0 gets an answer. The registry refuses a partial set,
        # which is the point: a record that answered four of six questions is not comparable with
        # one that answered all six, and nothing downstream would notice.
        return ModelResult(
            answers={
                "profile_consistent": ModelAnswer(
                    "profile_consistent", bool(state["profile_consistent"]), 0.86,
                    {True: 0.86, False: 0.14} if state["profile_consistent"] else {True: 0.14, False: 0.86},
                ),
                "structuring_pattern": ModelAnswer(
                    "structuring_pattern", bool(state["structuring_pattern"]), 0.81,
                ),
                "counterparty_risk": ModelAnswer(
                    "counterparty_risk", round(state["counterparty_risk"] * 3, 2), 0.74,
                ),
                "explanation_on_file": ModelAnswer(
                    "explanation_on_file", bool(state["explanation_on_file"]), 0.90,
                ),
                "behaviour_change": ModelAnswer(
                    "behaviour_change", round(state["behaviour_change"] * 3, 2), 0.69,
                ),
                "disposition": ModelAnswer(
                    "disposition", value, confidence,
                    {value: confidence, "escalate" if close else "close": round(1 - confidence, 2)},
                ),
            },
            model="scripted-1",
            tokens_in=1800,
            tokens_out=0,
        )


def build_model():
    """Jev if a key is configured, a scripted model otherwise. The caller is told which."""
    if not os.environ.get("TYPESAFE_API_KEY"):
        return ScriptedModel(), False
    from warrant.adapters.jev import JevModel

    return JevModel(model=JEV_MODEL), True


def sample_alert(rng, index: int) -> app.Alert:
    """Derived state only: ratios, bands and flags, never identifiers."""
    return app.Alert(
        alert_id=f"TM-{70000 + index}",
        segment=rng.choice(SEGMENTS),
        profile_consistent=rng.random() < 0.72,
        structuring_pattern=rng.random() < 0.12,
        counterparty_risk=round(rng.betavariate(1.5, 6.0), 2),
        explanation_on_file=rng.random() < 0.58,
        behaviour_change=round(rng.betavariate(1.5, 6.0), 2),
        amount_band=rng.choice(["under_1l", "1l_5l", "5l_20l", "over_20l"]),
    )


#: A fifth of alerts have no answer even at the horizon. Real books look like this, and a demo
#: showing every decision neatly resolved teaches the opposite of what the product is for.
UNOBSERVABLE_SHARE = 0.2


def realised_outcome(rng, confidence: float) -> str:
    """What actually happened, ninety days on. Overconfident above 0.90, honest below it.

    Returns an empty string when the system of record still cannot say, which leaves the decision
    outcome-pending rather than guessing.
    """
    if rng.random() < UNOBSERVABLE_SHARE:
        return ""
    true_rate = confidence if confidence < 0.9 else confidence - 0.12
    return "stayed_closed" if rng.random() < true_rate else "reopened"


async def main(alerts: int, store: str) -> None:
    model, live = build_model()
    print(
        f"decision model: {model.provider} "
        + (f"({JEV_MODEL}, live)" if live else "(scripted — the curve below proves nothing)")
    )

    # A DSN is not a path: only a local file gets cleared between runs.
    if not store.startswith(("postgres://", "postgresql://")) and Path(store).exists():
        Path(store).unlink()
    rng = random.Random(7)
    outcomes: dict = {}

    client = Warrant(
        "aml", tenant="demo-bank", store=store,
        agent=AgentInfo("alert-adjudicator", "1.4.0"),
        policy_bundle=POLICIES, currency="INR", flush_interval=0.02,
    )
    from warrant.questions import Registry

    app.wire(
        # The registry makes the pinned version enforceable: an unregistered set cannot run, and
        # an answer outside the permitted values is caught here rather than in a curve months on.
        adapter=DecisionAdapter(client, model, registry=Registry.load(QUESTION_SETS)),
        client=client,
        questions=build_questions(live),
        outcome_source=lambda alert_id: outcomes.get(alert_id, ""),
    )
    # The mapping is empty on purpose: the model adapter records the decision, and naming the
    # adjudicating activity here as well would record the same decision twice.
    interceptor = WarrantInterceptor(client, {}, workflow_only=True)

    reviewed = auto = 0
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client, task_queue="platform",
                workflows=[app.AlertAdjudication],
                activities=[app.adjudicate_alert, app.close_alert, app.look_up_outcome,
                            app.record_outcome, interceptor.record_activity],
                interceptors=[interceptor],
            ):
                handles = []
                for i in range(alerts):
                    alert = sample_alert(rng, i)
                    outcomes[alert.alert_id] = realised_outcome(rng, 0.95)
                    handles.append(
                        await env.client.start_workflow(
                            app.AlertAdjudication.run, alert,
                            id=f"alert-{alert.alert_id}", task_queue="platform",
                        )
                    )
                print(f"{alerts} alert(s) raised; waiting out a 90-day horizon on each")
                for handle in handles:
                    described = await handle.describe()
                    if described.status and described.status.name == "RUNNING":
                        # routed to a person: the case stays open until the bank answers
                        await handle.signal(
                            app.AlertAdjudication.reviewed,
                            args=["l2@bank.example", "approve", "reviewed in the demo"],
                        )
                for handle in handles:
                    result = await handle.result()
                    auto += result.route == "auto"
                    reviewed += result.route == "human"
        assert client.flush(timeout=10)
    finally:
        client.close()

    print(f"  {auto} auto-closed, {reviewed} sent to a person\n")
    report(store)


def build_questions(live: bool) -> dict:
    """The six-question set. Real Jev question objects when a key is configured."""
    if not live:
        return {"disposition": object()}
    from typesafe_sdk import Choice, Noul, Score
    from warrant.questions import Registry

    # Built from the registry, so the questions Jev is asked and the questions stamped on the
    # record are the same text by construction rather than by someone keeping two files in step.
    question_set = Registry.load(QUESTION_SETS).get(*QUESTION_SET)
    built = {}
    for name, q in question_set.questions.items():
        if q.primitive == "noul":
            built[name] = Noul(instructions=q.instructions)
        elif q.primitive == "choice":
            built[name] = Choice(instructions=q.instructions, criteria={c: None for c in q.criteria})
        else:
            built[name] = Score(instructions=q.instructions, criteria=list(q.criteria))
    return built


def report(store: str) -> None:
    """What a design partner is shown: the curve over the automated band, then the pack."""
    from warrant.calibrate import calibrate
    from warrant.outcomes import coverage
    from warrant.pack import build_pack

    reader = open_store(store, read_only=True)
    try:
        print(coverage(reader, stream="aml").summary(), "\n")
        report = calibrate(
            reader, stream="aml",
            correct_when="outcome.label == 'stayed_closed'",
            where="decision.route == 'auto'",
            answer="disposition",
        )
        print(report.summary(), "\n")
    finally:
        reader.close()

    pack_dir = HERE / "pack"
    if pack_dir.exists():
        shutil.rmtree(pack_dir)
    reader = open_store(store, read_only=True)
    try:
        result = build_pack(
            reader, pack_dir, stream="aml", policy_dir=POLICIES, questions_dir=QUESTION_SETS,
            correct_when="outcome.label == 'stayed_closed'",
            where="decision.route == 'auto'", answer="disposition",
            title="Alert adjudication: evidence pack",
        )
    finally:
        reader.close()
    print(result.summary())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alerts", type=int, default=60, help="how many to run (default 60)")
    parser.add_argument("--store", default=str(HERE / "records.db"),
                        help="a local SQLite path, or a postgresql:// DSN")
    args = parser.parse_args()
    asyncio.run(main(args.alerts, args.store))
