"""TypeSafe Jev behind the decision-model boundary, and one call that decides and records together.

If a developer can call Jev without producing a record, the ledger is incomplete and the product
does not work. So this module offers the ergonomics the application wants — one call in, answers
and a route out — while the decision record is written as part of the same operation rather than
as a thing someone remembers to do afterwards.

Three rules worth knowing before changing anything here.

**A pinned model version is required.** TypeSafe's default is ``jev-latest``, an alias that moves
when they publish. A reliability curve measured against an alias describes a model that may no
longer exist, and their own documentation says to pin when confidence thresholds have been tuned
against a version. Passing an alias raises unless the caller explicitly accepts it, and the
acceptance is recorded on every record it produces.

**Where inference happened is recorded on every record.** Residency is a regulated question in the
markets this is built for, and it cannot be reconstructed after the fact. The endpoint rides along
as evidence; a decision class may also declare that it must not leave a region, and the adapter
refuses to send rather than discovering the problem in an audit.

**No Jev vocabulary reaches the record schema.** Nouls, choices and scores are normalised to a
value, a confidence and a distribution before anything is written. See :mod:`warrant.adapters.base`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional, Sequence

from warrant.adapters.base import DecisionModel, ModelAnswer, ModelError, ModelResult
from warrant.hashing import canonical_json, content_hash

log = logging.getLogger("warrant.adapters.jev")

PROVIDER = "typesafe"
ALIASES = ("jev-latest", "jev-preview")
DEFAULT_ENDPOINT = "https://api.typesafe.ai"
#: Regions we can positively identify from an endpoint. Anything else is reported as "unknown",
#: which is a finding rather than a default to be waved through.
KNOWN_REGIONS = {"api.typesafe.ai": "global"}


class ResidencyError(RuntimeError):
    """A decision class requires a region this endpoint cannot satisfy. Raised before any send."""


def region_of(endpoint: str) -> str:
    """The region an endpoint serves, or ``unknown``. Never guesses a region it cannot identify."""
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].lower()
    return KNOWN_REGIONS.get(host, "unknown")


def _require_sdk():
    try:
        import typesafe_sdk
    except ImportError as exc:  # pragma: no cover - exercised by the install matrix, not the suite
        raise ImportError(
            'the Jev adapter needs the TypeSafe SDK: pip install "warrantai[jev]"'
        ) from exc
    return typesafe_sdk


def normalise_answer(question: str, answer: Any) -> ModelAnswer:
    """Turn one Jev answer into the vendor-neutral shape, whichever primitive produced it.

    A Noul carries no confidence of its own: it is a single probability where 0.5 means "no idea".
    That *is* a two-outcome distribution, so it becomes a boolean value, a confidence equal to the
    winning side's probability, and the distribution it always was. Doing anything else would make
    a yes/no question the one primitive calibration cannot measure.
    """
    kind = getattr(answer, "type", None)
    if kind == "noul":
        p_true = float(answer.noul)
        return ModelAnswer(
            question=question,
            value=p_true >= 0.5,
            confidence=max(p_true, 1.0 - p_true),
            distribution={True: p_true, False: round(1.0 - p_true, 12)},
        )
    if kind == "choice":
        return ModelAnswer(
            question=question,
            value=answer.choice,
            confidence=float(answer.confidence),
            distribution={k: float(v) for k, v in (answer.probabilities or {}).items()},
        )
    if kind == "score":
        return ModelAnswer(
            question=question,
            value=float(answer.score),
            confidence=float(answer.confidence),
            distribution={k: float(v) for k, v in (answer.probabilities or {}).items()},
        )
    raise ModelError(f"unknown answer primitive {kind!r} for question {question!r}")


class JevModel(DecisionModel):
    """TypeSafe's client behind :class:`~warrant.adapters.base.DecisionModel`.

    ``model`` must be a pinned version such as ``jev-1.13.0``. Passing ``jev-latest`` or
    ``jev-preview`` raises unless ``allow_alias=True``, because an alias moves underneath a
    calibration curve without telling anyone.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Any = None,
        allow_alias: bool = False,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a pinned version string, e.g. 'jev-1.13.0'")
        if model in ALIASES and not allow_alias:
            raise ValueError(
                f"{model!r} is an alias that moves when TypeSafe publishes a new version, so a "
                "reliability curve measured against it describes a model that may no longer exist. "
                "Pin a version such as 'jev-1.13.0', or pass allow_alias=True and accept that "
                "calibration cannot be attributed to a model version."
            )
        self.model = model
        self.pinned = model not in ALIASES
        self._endpoint = base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_ENDPOINT
        if client is not None:
            self._client = client
        else:
            sdk = _require_sdk()
            kwargs: Dict[str, Any] = {"model": model}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            if timeout is not None:
                kwargs["timeout"] = timeout
            self._client = sdk.TypeSafeClient(**kwargs)
        if not self.pinned:
            log.warning(
                "jev model %s is an alias; records cannot attribute calibration to a version", model
            )

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def region(self) -> str:
        return region_of(self._endpoint)

    def evaluate(self, state: Any, questions: Mapping[str, Any]) -> ModelResult:
        if not questions:
            raise ModelError("no questions to evaluate")
        try:
            response = self._client.system_one(state, dict(questions), model=self.model)
        except Exception as exc:
            # The vendor's error text can quote the state, which may carry personal data. Record
            # the class, never the message — the same rule the other adapters follow.
            raise ModelError(f"{type(exc).__name__} from the decision model") from exc

        answers = {name: normalise_answer(name, a) for name, a in response.answers.items()}
        usage = getattr(response, "usage", None)
        return ModelResult(
            answers=answers,
            model=getattr(response, "model", self.model) or self.model,
            tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
            tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
            extra={"endpoint": self._endpoint, "region": self.region, "pinned": self.pinned},
        )


class JevResult:
    """What one :meth:`JevAdapter.decide` produced: the answers, the route, and the record id."""

    __slots__ = ("answers", "route", "record_id", "verdict", "model", "state_digest")

    def __init__(self, *, answers, route, record_id, verdict, model, state_digest) -> None:
        self.answers: Dict[str, ModelAnswer] = answers
        self.route: str = route
        self.record_id: str = record_id
        self.verdict = verdict
        self.model: str = model
        self.state_digest: str = state_digest

    def __getitem__(self, question: str) -> ModelAnswer:
        return self.answers[question]

    @property
    def allowed(self) -> bool:
        return self.route == "auto"


def canonical_state(state: Any) -> str:
    """Serialise state deterministically, so the digest is of exactly what the model was sent."""
    if isinstance(state, str):
        return state
    return canonical_json(state)


class JevAdapter:
    """One call that decides and records together.

    ``persist`` and ``on_ledger_unavailable`` are per decision class. The defaults match the SDK:
    recording is asynchronous and a ledger outage never blocks the caller. A class that must not
    act without a sealed record sets ``persist={"aml.alert.disposition": "sync"}`` and, if it must
    not act at all when the ledger is unreachable, ``on_ledger_unavailable={...: "closed"}``.
    Whichever is in force is recorded, so the configuration is auditable after the fact.
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
            (self._persist, ("sync", "async"), "persist"),
            (self._fail, ("open", "closed"), "on_ledger_unavailable"),
        ):
            for cls, value in mapping.items():
                if value not in allowed:
                    raise ValueError(f"{label}[{cls!r}] must be one of {', '.join(allowed)}, got {value!r}")

    def _check_residency(self, decision_class: str) -> None:
        required = self._residency.get(decision_class)
        if required is None:
            return
        actual = getattr(self._model, "region", region_of(self._model.endpoint))
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
    ) -> JevResult:
        """Evaluate ``questions`` about ``state``, check the mandate, and record the decision.

        ``question_set`` is ``(id, version)`` and is stamped on the record so a reliability curve
        can be scoped to one revision. ``disposition`` names the answer whose value is the action
        and whose confidence the policy routes on.
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

        # Everything that can fail on bad input has failed by now. Only from here does a decision
        # scope open, because a malformed call is not a decision and must not become a record.
        with self._w.decide(decision_class, subject=subject) as d:
            if question_set is not None:
                d.question_set(*question_set)
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

            try:
                result = self._model.evaluate(state, questions)
            except ModelError:
                raise  # a failed evaluation is a failed decision; __exit__ records it as such

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
            d.state(digest=digest, ref=state_ref)

            chosen = result.answers.get(disposition)
            if chosen is None:
                raise ModelError(
                    f"the model did not answer {disposition!r}; pass disposition= to name the "
                    "answer the policy routes on"
                )
            inputs = dict(check_inputs) if check_inputs is not None else self._default_inputs(state, result, disposition)
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
            "jev decision %s for %s: route=%s model=%s", decision_class, subject, route, result.model
        )
        return JevResult(
            answers=result.answers,
            route=route,
            record_id=record_id,
            verdict=verdict,
            model=result.model,
            state_digest=digest,
        )

    @staticmethod
    def _default_inputs(state: Any, result: ModelResult, disposition: str) -> Dict[str, Any]:
        """Scalars a policy clause can read: the state's own fields plus the answers."""
        inputs: Dict[str, Any] = {}
        if isinstance(state, Mapping):
            inputs.update({k: v for k, v in state.items() if isinstance(v, (str, int, float, bool))})
        for name, answer in result.answers.items():
            inputs[name] = answer.value
        chosen = result.answers[disposition]
        inputs[disposition] = chosen.value
        inputs["confidence"] = chosen.confidence if chosen.confidence is not None else 0.0
        return inputs

    def _flush_or_fail(self, decision_class: str, record_id: str) -> None:
        delivered = self._w.flush(timeout=self._flush_timeout)
        if delivered:
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


class LedgerUnavailable(RuntimeError):
    """A fail-closed decision class could not seal its record, so the decision must not be used."""
