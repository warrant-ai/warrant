"""The one boundary a decision model may cross.

Every model that answers typed questions reaches Warrant through :class:`DecisionModel`. Nothing
above this line knows which vendor answered, and nothing below it knows what a decision record is.
That is what lets a vendor be replaced without touching the ledger, and it is why no vendor's
vocabulary appears in the decision record schema.

The protocol is deliberately small. A model takes state and a set of questions, and returns typed
answers with — where the primitive supports it — a probability distribution rather than only the
winning value. Everything else Warrant needs (hashing, policy, sealing, cost) happens on this side
of the line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Union

AnswerValue = Union[str, float, bool, int]


class ModelError(RuntimeError):
    """A decision model could not answer. Carries no vendor error text, which may hold state."""


@dataclass(frozen=True)
class ModelAnswer:
    """One typed answer, normalised across whatever primitives the vendor offers.

    ``distribution`` maps each candidate value to its probability. Reliability analysis is
    impossible without the runners-up, so an adapter that has them must pass them through, and one
    that does not must say so by leaving this ``None`` rather than inventing a point mass.
    """

    question: str
    value: AnswerValue
    confidence: Optional[float] = None
    distribution: Optional[Dict[AnswerValue, float]] = None

    def as_record_answer(self) -> Dict[str, Any]:
        """The shape ``Decision.answer()`` writes onto the record."""
        item: Dict[str, Any] = {"question": self.question, "value": self.value}
        if self.confidence is not None:
            item["confidence"] = self.confidence
        if self.distribution:
            item["distribution"] = [{"value": v, "p": p} for v, p in self.distribution.items()]
        return item


@dataclass(frozen=True)
class ModelResult:
    """Everything one evaluation produced, including what it cost and exactly which model ran."""

    answers: Dict[str, ModelAnswer]
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, question: str) -> ModelAnswer:
        return self.answers[question]


class DecisionModel(Protocol):
    """Anything that answers typed questions about a state.

    Implementations are interchangeable: swapping one for another must require no change outside
    ``warrant.adapters``. The test suite holds a fake alongside the real vendor client to keep
    that true rather than assumed.
    """

    @property
    def provider(self) -> str:
        """Short vendor label recorded as the evidence provider, e.g. ``typesafe``."""
        ...

    @property
    def endpoint(self) -> str:
        """Base URL the evaluation is sent to. Recorded, because where inference happened is a
        regulated question and cannot be reconstructed after the fact."""
        ...

    def evaluate(self, state: Any, questions: Mapping[str, Any]) -> ModelResult:
        """Answer ``questions`` about ``state``. Raises :class:`ModelError` on failure."""
        ...


def ordered_answers(result: ModelResult, order: Optional[Sequence[str]] = None) -> Sequence[ModelAnswer]:
    """Answers in a stable order, so two records of the same decision compare cleanly."""
    if order is None:
        return [result.answers[name] for name in sorted(result.answers)]
    missing = [name for name in order if name not in result.answers]
    if missing:
        raise ModelError(f"the model did not answer: {', '.join(missing)}")
    return [result.answers[name] for name in order]
