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
from warrant.adapters.model import (
    DecisionAdapter,
    DecisionResult,
    LedgerUnavailable,
    ResidencyError,
    canonical_state,
)

log = logging.getLogger("warrant.adapters.jev")

PROVIDER = "typesafe"
ALIASES = ("jev-latest", "jev-preview")
DEFAULT_ENDPOINT = "https://api.typesafe.ai"
#: Regions we can positively identify from an endpoint. Anything else is reported as "unknown",
#: which is a finding rather than a default to be waved through.
KNOWN_REGIONS = {"api.typesafe.ai": "global"}


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


#: The adapter is vendor-neutral and lives in :mod:`warrant.adapters.model`. These names exist so a
#: reader who arrived here looking for the Jev integration finds the whole of it in one place.
JevAdapter = DecisionAdapter
JevResult = DecisionResult

__all__ = [
    "ALIASES",
    "DEFAULT_ENDPOINT",
    "DecisionAdapter",
    "DecisionResult",
    "JevAdapter",
    "JevModel",
    "JevResult",
    "LedgerUnavailable",
    "ResidencyError",
    "canonical_state",
    "normalise_answer",
    "region_of",
]
