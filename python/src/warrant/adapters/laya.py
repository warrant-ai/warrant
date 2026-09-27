"""Convai's Laya behind the decision-model boundary: typed decisions that never leave the host.

Laya is an open-weight System One decision model (Apache 2.0, Convai Innovations, Kerala). It
answers the same Noul, Choice and Score questions as TypeSafe's Jev, but it runs in-process from
weights on local disk, so the state it reads never crosses a network. That is the whole reason it
is here: a regulated Indian lender cannot send customer state to a model hosted abroad, and it can
run this one inside its own perimeter.

Three rules worth knowing before changing anything here, each the same lesson Jev taught.

**A pinned revision is required.** Laya's weights live on Hugging Face under a branch that moves
when Convai publishes, and ``laya.load`` has no revision argument. So the adapter downloads the
exact commit it was given and loads from that local path. A reliability curve measured against a
moving branch describes a model that may no longer exist; passing no revision raises unless the
caller accepts that with ``allow_unpinned=True``.

**The model identifier is ours, not Laya's.** Every checkpoint and every version reports the same
constant, ``laya-rl-agent``, which cannot say what answered. The record carries
``laya@<sha[:12]>/<checkpoint>`` instead.

**A field called confidence is not necessarily a confidence.** Laya's ``confidence`` is normalised
entropy, ``1 - H(p)/log k``: how concentrated the whole distribution is, on a different scale from a
probability. Its own documentation says not to threshold on it. Warrant's ``confidence`` means the
stated probability that ``value`` is right, so a Choice takes ``probabilities[chosen]``, a Noul
takes the winning side's probability, and a Score states none. See :func:`normalise_answer`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Mapping, Optional

from warrant.adapters.base import DecisionModel, ModelAnswer, ModelError, ModelResult
from warrant.adapters.model import (
    IN_PROCESS,
    DecisionAdapter,
    DecisionResult,
    LedgerUnavailable,
    ResidencyError,
    canonical_state,
)

log = logging.getLogger("warrant.adapters.laya")

PROVIDER = "convai-laya"
DEFAULT_REPO = "convaiinnovations/laya"
ENDPOINT = "local://laya"
#: Inference runs in this process, on this host. It never leaves, so it satisfies any residency
#: requirement a decision class can state; see ``DecisionAdapter._check_residency``.
REGION = IN_PROCESS
PRIMITIVES = ("noul", "choice", "score")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
#: What a checkpoint needs: its config, weights and tokenizer. Not the benchmark images.
_WEIGHT_PATTERNS = ("*.json", "*.safetensors", "*.txt", "*.model", "tokenizer/*", "encoder/*")


def _require_laya():
    try:
        import laya
    except ImportError as exc:  # pragma: no cover - exercised by the install matrix, not the suite
        raise ImportError('the Laya adapter needs the laya package: pip install "warrantai[laya]"') from exc
    return laya


def to_laya_question(name: str, spec: Any) -> Dict[str, Any]:
    """One question in Laya's form ``{type, instructions, criteria}``, from whatever the caller has.

    Accepts a :class:`warrant.questions.Question`, its dict form (``primitive``), or a dict already
    in Laya's form (``type``). Anything else is refused here, before a decision scope opens.
    """
    if hasattr(spec, "primitive") and hasattr(spec, "instructions"):
        kind, instructions, criteria = spec.primitive, spec.instructions, getattr(spec, "criteria", ())
    elif isinstance(spec, Mapping):
        kind = spec.get("type", spec.get("primitive"))
        instructions, criteria = spec.get("instructions"), spec.get("criteria")
    else:
        raise ValueError(
            f"question {name!r}: expected a Question or a dict with 'primitive' or 'type', "
            f"got {type(spec).__name__}"
        )
    if kind not in PRIMITIVES:
        raise ValueError(f"question {name!r}: primitive must be one of {', '.join(PRIMITIVES)}, got {kind!r}")
    if not isinstance(instructions, str) or not instructions:
        raise ValueError(f"question {name!r}: instructions must be a non-empty string")
    out: Dict[str, Any] = {"type": kind, "instructions": instructions}
    if kind == "choice":
        if isinstance(criteria, Mapping):
            options = dict(criteria)
        else:
            options = {str(c): None for c in (criteria or ())}
        if not options:
            raise ValueError(f"question {name!r}: a choice needs at least one criterion")
        out["criteria"] = options
    elif kind == "score":
        levels = list(criteria or ())
        if not levels:
            raise ValueError(f"question {name!r}: a score needs at least one level")
        out["criteria"] = levels
    elif criteria:
        if not isinstance(criteria, Mapping) or not {str(k).lower() for k in criteria} <= {"true", "false"}:
            raise ValueError(f"question {name!r}: a noul's criteria may only describe 'true' and 'false'")
        out["criteria"] = {str(k).lower(): v for k, v in criteria.items()}
    return out


def normalise_answer(question: str, answer: Mapping[str, Any]) -> ModelAnswer:
    """Turn one Laya answer into the vendor-neutral shape.

    ``confidence`` on a Warrant record is the stated probability that ``value`` is right. Laya's
    field of that name is normalised entropy and is never copied: on a two-way choice at
    ``{escalate: 0.65, close: 0.35}`` it reports about 0.06, which as a confidence would hold every
    policy floor shut. ``answer_confidence`` is max(p), which equals p(chosen), and is used only if
    the distribution is missing.

    A Score is an expectation over buckets, not a value an outcome can equal, so it states no
    confidence, exactly as the Jev adapter does.
    """
    kind = answer.get("type")
    probabilities = answer.get("probabilities") or {}
    if kind == "noul":
        p_true = float(answer["noul"])
        return ModelAnswer(
            question=question,
            value=p_true >= 0.5,
            confidence=max(p_true, 1.0 - p_true),
            distribution={True: p_true, False: round(1.0 - p_true, 12)},
        )
    if kind == "choice":
        distribution = {str(k): float(v) for k, v in probabilities.items()}
        chosen = answer["choice"]
        p_chosen = distribution.get(chosen)
        if p_chosen is None:
            fallback = answer.get("answer_confidence")
            p_chosen = float(fallback) if fallback is not None else None
        return ModelAnswer(
            question=question,
            value=chosen,
            confidence=p_chosen,
            distribution=distribution or None,
        )
    if kind == "score":
        return ModelAnswer(
            question=question,
            value=float(answer["score"]),
            confidence=None,
            distribution={int(k): float(v) for k, v in probabilities.items()} or None,
        )
    raise ModelError(f"unknown answer primitive {kind!r} for question {question!r}")


class LayaModel(DecisionModel):
    """Laya behind :class:`~warrant.adapters.base.DecisionModel`, running in this process.

    ``revision`` must be a 40-character Hugging Face commit sha. ``subfolder`` picks a checkpoint
    from the repo, e.g. ``"multilingual"`` for Hindi, Bengali, Tamil, Telugu and Kannada; the
    repo root is the English one. ``agent`` injects a loaded Laya agent (tests, or a caller that
    manages its own weights), in which case nothing is downloaded.
    """

    def __init__(
        self,
        *,
        revision: Optional[str] = None,
        subfolder: Optional[str] = None,
        repo: str = DEFAULT_REPO,
        device: Optional[str] = None,
        agent: Any = None,
        allow_unpinned: bool = False,
    ) -> None:
        pinned = isinstance(revision, str) and bool(_SHA_RE.match(revision))
        if not pinned and not allow_unpinned:
            raise ValueError(
                f"revision must be a 40-character Hugging Face commit sha, got {revision!r}. Laya's "
                "weights sit on a branch that moves when Convai publishes, so a reliability curve "
                "measured without a pin describes a model that may no longer exist. Pin a commit, "
                "or pass allow_unpinned=True and accept that calibration cannot be attributed."
            )
        if subfolder is not None and (not isinstance(subfolder, str) or not subfolder):
            raise ValueError("subfolder must be a non-empty string or None")
        self.revision = revision if pinned else None
        self.pinned = pinned
        self.subfolder = subfolder
        self.repo = repo
        self.model = f"laya@{self.revision[:12] if self.revision else 'unpinned'}/{subfolder or 'english'}"
        if agent is not None:
            self._agent = agent
        else:
            laya = _require_laya()
            from huggingface_hub import snapshot_download

            patterns = list(_WEIGHT_PATTERNS)
            if subfolder:
                patterns += [f"{subfolder}/*", f"{subfolder}/**/*"]
            path = snapshot_download(repo, revision=self.revision, allow_patterns=patterns)
            kwargs: Dict[str, Any] = {"subfolder": subfolder} if subfolder else {}
            if device is not None:
                kwargs["device"] = device
            self._agent = laya.load(path, **kwargs)
        if not self.pinned:
            log.warning("laya model %s is unpinned; records cannot attribute calibration to a version", self.model)

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def endpoint(self) -> str:
        return ENDPOINT

    @property
    def region(self) -> str:
        return REGION

    def evaluate(self, state: Any, questions: Mapping[str, Any]) -> ModelResult:
        if not questions:
            raise ModelError("no questions to evaluate")
        try:
            laya_questions = {name: to_laya_question(name, spec) for name, spec in questions.items()}
        except ValueError as exc:
            raise ModelError(str(exc)) from exc
        payload = state if isinstance(state, (str, dict, list)) else canonical_state(state)
        try:
            response = self._agent.system_one(payload, laya_questions)
        except Exception as exc:
            # A model error can quote the state. Record the class, never the message.
            raise ModelError(f"{type(exc).__name__} from the decision model") from exc

        answers = {name: normalise_answer(name, a) for name, a in (response.get("answers") or {}).items()}
        usage = response.get("usage") or {}
        return ModelResult(
            answers=answers,
            model=self.model,
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            extra={"endpoint": ENDPOINT, "region": REGION, "pinned": self.pinned, "revision": self.revision},
        )


LayaAdapter = DecisionAdapter
LayaResult = DecisionResult

__all__ = [
    "DEFAULT_REPO",
    "DecisionAdapter",
    "DecisionResult",
    "ENDPOINT",
    "LayaAdapter",
    "LayaModel",
    "LayaResult",
    "LedgerUnavailable",
    "REGION",
    "ResidencyError",
    "normalise_answer",
    "to_laya_question",
]
