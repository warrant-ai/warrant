"""One call that decides and records together, for any model behind :class:`DecisionModel`.

If a developer can call the model without producing a record, the ledger is incomplete and the
product does not work. So this offers the ergonomics an application wants — state and questions in,
answers and a route out — and writes the decision record as part of the same operation rather than
as something someone remembers to do afterwards.

Nothing here names a vendor. ``warrant.adapters.jev`` supplies TypeSafe's Jev as the first
implementation; a second one changes no line in this file, which is the property that makes
depending on a days-old vendor survivable.

**Composing with Temporal.** When ``decide()`` runs inside a Temporal activity it picks that up on
its own: the execution lands on the record as ``temporal.execution`` evidence, and the record id is
derived from the attempt's identity so a re-sent batch dedupes while a retry is a new record —
exactly as ``warrant.adapters.temporal`` does. The activity that makes a model decision must
therefore **not** also be listed in that adapter's ``ToolDecision`` mapping, or the same decision
would be recorded twice. The model adapter owns the record because it holds the answers, the
confidences and the state digest; Temporal contributes the identity.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Sequence

from warrant.adapters.base import DecisionModel, ModelError, ModelResult
from warrant.hashing import canonical_json, content_hash
from warrant.ids import deterministic_ulid

log = logging.getLogger("warrant.adapters.model")

PERSIST_MODES = ("sync", "async")
UNAVAILABLE_MODES = ("open", "closed")


class ResidencyError(RuntimeError):
    """A decision class requires a region this endpoint cannot satisfy. Raised before any send."""


class LedgerUnavailable(RuntimeError):
    """A fail-closed decision class could not seal its record, so the decision must not be used."""


def canonical_state(state: Any) -> str:
    """Serialise state deterministically, so the digest covers exactly what the model was sent."""
    if isinstance(state, str):
        return state
    return canonical_json(state)


def _temporal_context() -> Optional[Dict[str, Any]]:
    """Identity of the Temporal activity we are inside, or ``None`` when we are not inside one.

    Imported lazily and tolerantly: the overwhelming majority of callers never run under Temporal,
    and an optional extra must not become a hard dependency of every decision.
    """
    try:
        from temporalio import activity
    except ImportError:
        return None
    try:
        info = activity.info()
    except RuntimeError:
        return None  # not in an activity context
    fields = {
        "namespace": info.namespace,
        "workflow_type": info.workflow_type,
        "workflow_id": info.workflow_id,
        "workflow_run_id": info.workflow_run_id,
        "activity_type": info.activity_type,
        "activity_id": info.activity_id,
        "attempt": info.attempt,
        "task_queue": info.task_queue,
    }
    uri = (
        f"temporal://{info.namespace}/{info.workflow_id}/{info.workflow_run_id}"
        f"/activity/{info.activity_id}?attempt={info.attempt}"
        f"&type={info.activity_type}&queue={info.task_queue}"
    )
    key = (
        f"{info.namespace}|{info.workflow_id}|{info.workflow_run_id}"
        f"|{info.activity_id}|{info.attempt}"
    )
    return {
        "uri": uri,
        "fields": fields,
        "record_id": deterministic_ulid(
            int(info.current_attempt_scheduled_time.timestamp() * 1000), key
        ),
    }


class DecisionResult:
    """What one :meth:`DecisionAdapter.decide` produced."""

    __slots__ = ("answers", "route", "record_id", "verdict", "model", "state_digest")

    def __init__(self, *, answers, route, record_id, verdict, model, state_digest) -> None:
        self.answers = answers
        self.route: str = route
        self.record_id: str = record_id
        self.verdict = verdict
        self.model: str = model
        self.state_digest: str = state_digest

    def __getitem__(self, question: str):
        return self.answers[question]

    @property
    def allowed(self) -> bool:
        return self.route == "auto"


class DecisionAdapter:
    """Evaluate typed questions, check the mandate, and record the decision, in one call.

    ``persist`` and ``on_ledger_unavailable`` are per decision class. The defaults match the SDK:
    recording is asynchronous and a ledger outage never blocks the caller. A class that must not act
    without a sealed record sets ``persist={cls: "sync"}``, and one that must not act at all when
    the ledger is unreachable adds ``on_ledger_unavailable={cls: "closed"}``. Whichever is in force
    rides on every record, so the configuration is auditable after the fact.

    ``residency`` names the region a class's inference must happen in. The adapter refuses to send
    rather than discovering the problem in an audit.
    """

    def __init__(
        self,
        warrant: Any,
        model: DecisionModel,
        *,
        persist: Optional[Mapping[str, str]] = None,
        on_ledger_unavailable: Optional[Mapping[str, str]] = None,
        residency: Optional[Mapping[str, str]] = None,
        flush_timeout: float = 5.0,
    ) -> None:
        self._w = warrant
        self._model = model
        self._persist = dict(persist or {})
        self._fail = dict(on_ledger_unavailable or {})
        self._residency = dict(residency or {})
        self._flush_timeout = flush_timeout
        for mapping, allowed, label in (
            (self._persist, PERSIST_MODES, "persist"),
            (self._fail, UNAVAILABLE_MODES, "on_ledger_unavailable"),
        ):
            for cls, value in mapping.items():
                if value not in allowed:
                    raise ValueError(
                        f"{label}[{cls!r}] must be one of {', '.join(allowed)}, got {value!r}"
                    )

    @property
    def model(self) -> DecisionModel:
        return self._model

    def _check_residency(self, decision_class: str) -> None:
        required = self._residency.get(decision_class)
        if required is None:
            return
        actual = getattr(self._model, "region", "unknown")
        if actual != required:
            raise ResidencyError(
                f"{decision_class} requires inference in region {required!r}, but "
                f"{self._model.endpoint} serves {actual!r}. Nothing was sent."
            )

    def decide(
        self,
        *,
        decision_class: str,
        subject: str,
        state: Any,
        questions: Mapping[str, Any],
        question_set: Optional[Sequence[str]] = None,
        disposition: str = "disposition",
        cost_centre: Optional[str] = None,
        state_ref: Optional[str] = None,
        check_inputs: Optional[Mapping[str, Any]] = None,
    ) -> DecisionResult:
        """Evaluate ``questions`` about ``state``, check the mandate, and record the decision.

        ``question_set`` is ``(id, version)``, stamped on the record so a reliability curve can be
        scoped to one revision. ``disposition`` names the answer whose value becomes the action and
        whose confidence the policy routes on.
        """
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        if not questions:
            raise ValueError("no questions to evaluate")
        if question_set is not None and len(tuple(question_set)) != 2:
            raise ValueError("question_set must be (id, version)")
        self._check_residency(decision_class)

        serialised = canonical_state(state)
        digest = content_hash(serialised)
        temporal = _temporal_context()

        # Everything that can fail on bad input has failed by now. Only from here does a decision
        # scope open, because a malformed call is not a decision and must not become a record.
        with self._w.decide(
            decision_class,
            subject=subject,
            record_id=temporal["record_id"] if temporal else None,
        ) as d:
            if question_set is not None:
                d.question_set(*question_set)
            d.state(digest=digest, ref=state_ref)
            d.evidence(
                "model.endpoint",
                uri=f"{self._model.endpoint}?region={getattr(self._model, 'region', 'unknown')}",
                type="other",
                content=f"{self._model.provider}:{getattr(self._model, 'model', '')}",
            )
            d.evidence(
                "warrant.persistence",
                uri=f"warrant://persistence?mode={self._persist.get(decision_class, 'async')}"
                f"&on_unavailable={self._fail.get(decision_class, 'open')}",
                type="other",
                content=decision_class,
            )
            if temporal is not None:
                d.evidence(
                    "temporal.execution", uri=temporal["uri"], type="other", content=temporal["fields"]
                )

            result = self._model.evaluate(state, questions)

            for answer in (result.answers[name] for name in sorted(result.answers)):
                d.answer(
                    answer.question,
                    answer.value,
                    confidence=answer.confidence,
                    distribution=answer.distribution,
                )
            d.model_call(
                self._model.provider,
                result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                content=serialised,
            )

            chosen = result.answers.get(disposition)
            if chosen is None:
                raise ModelError(
                    f"the model did not answer {disposition!r}; pass disposition= to name the "
                    "answer the policy routes on"
                )
            inputs = (
                dict(check_inputs)
                if check_inputs is not None
                else self._default_inputs(state, result, disposition)
            )
            verdict = d.check(**inputs)
            route = "auto" if verdict.allowed else "human"
            if not verdict.allowed:
                d.require_human(note=f"mandate result {verdict.result}")
            d.act(
                str(chosen.value) if verdict.allowed else "refer",
                cost_centre=cost_centre,
                route=route,
            )
            record_id = d.record_id

        if self._persist.get(decision_class) == "sync":
            self._flush_or_fail(decision_class, record_id)

        log.info(
            "decision %s for %s: route=%s model=%s%s",
            decision_class,
            subject,
            route,
            result.model,
            " (temporal)" if temporal else "",
        )
        return DecisionResult(
            answers=result.answers,
            route=route,
            record_id=record_id,
            verdict=verdict,
            model=result.model,
            state_digest=digest,
        )

    @staticmethod
    def _default_inputs(state: Any, result: ModelResult, disposition: str) -> Dict[str, Any]:
        """Scalars a policy clause can read: the state's own fields, then the answers."""
        inputs: Dict[str, Any] = {}
        if isinstance(state, Mapping):
            inputs.update(
                {k: v for k, v in state.items() if isinstance(v, (str, int, float, bool))}
            )
        for name, answer in result.answers.items():
            inputs[name] = answer.value
        chosen = result.answers[disposition]
        inputs[disposition] = chosen.value
        inputs["confidence"] = chosen.confidence if chosen.confidence is not None else 0.0
        return inputs

    def _flush_or_fail(self, decision_class: str, record_id: str) -> None:
        if self._w.flush(timeout=self._flush_timeout):
            return
        if self._fail.get(decision_class) == "closed":
            raise LedgerUnavailable(
                f"{decision_class} is configured to fail closed and record {record_id} could not "
                f"be sealed within {self._flush_timeout}s. The decision must not be acted on."
            )
        log.error(
            "record %s for %s not sealed within %.1fs; it is spooled and will be retried",
            record_id,
            decision_class,
            self._flush_timeout,
        )
