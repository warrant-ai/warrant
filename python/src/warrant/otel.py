"""Automatic evidence capture from OpenTelemetry generative-AI spans.

Install the extra (``pip install "warrantai[otel]"``) and register the processor on
your tracer provider::

    from opentelemetry.sdk.trace import TracerProvider
    from warrant.otel import WarrantSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(WarrantSpanProcessor(pricer=my_price_table))

Any span that starts while a decision is open and carries generative-AI semantic
convention attributes is attached to that decision when it ends: model calls
(``gen_ai.operation.name`` of chat, text_completion, generate_content or
embeddings, or a ``gen_ai.request.model``) become ``model_call`` evidence with
token usage; tool executions (``gen_ai.operation.name`` of ``execute_tool`` or a
``gen_ai.tool.name``) become ``tool_call`` evidence. Evidence points at the span by
trace and span id, and its content hash covers the span attributes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from warrant.client import Decision, current_decision

try:
    from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('OpenTelemetry capture needs the opentelemetry-sdk package: pip install "warrantai[otel]"') from exc

log = logging.getLogger("warrant.otel")

Pricer = Callable[[str, str, int, int], float]
"""``(provider, model, tokens_in, tokens_out) -> amount`` in the client's currency."""

_MODEL_OPERATIONS = ("chat", "text_completion", "generate_content", "embeddings")
_TOOL_OPERATIONS = ("execute_tool",)


class WarrantSpanProcessor(SpanProcessor):
    """Attach generative-AI spans to the decision that was open when they started."""

    def __init__(self, pricer: Optional[Pricer] = None) -> None:
        self._pricer = pricer
        self._lock = threading.Lock()
        self._open: Dict[Tuple[int, int], Decision] = {}

    def on_start(self, span: Span, parent_context=None) -> None:
        decision = current_decision()
        if decision is None:
            return
        ctx = span.get_span_context()
        with self._lock:
            self._open[(ctx.trace_id, ctx.span_id)] = decision

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        with self._lock:
            decision = self._open.pop((ctx.trace_id, ctx.span_id), None)
        if decision is None:
            return
        attrs: Mapping[str, Any] = span.attributes or {}
        kind = classify(attrs)
        if kind is None:
            return
        uri = f"otel://trace/{ctx.trace_id:032x}/span/{ctx.span_id:016x}"
        content = {"name": span.name, "attributes": dict(attrs)}
        try:
            if kind == "model_call":
                provider = _first(attrs, "gen_ai.provider.name", "gen_ai.system") or "unknown"
                model = _first(attrs, "gen_ai.response.model", "gen_ai.request.model") or "unknown"
                tokens_in = _int(_first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
                tokens_out = _int(_first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
                amount = self._price(provider, model, tokens_in, tokens_out)
                decision.model_call(provider, model, tokens_in=tokens_in, tokens_out=tokens_out, amount=amount, uri=uri, content=content)
            else:
                name = _first(attrs, "gen_ai.tool.name") or span.name
                decision.tool_call(str(name), uri=uri, content=content)
        except RuntimeError as exc:
            log.debug("warrant: span %s ended after its decision closed; not attached (%s)", span.name, exc)
        except ValueError as exc:
            log.warning("warrant: could not attach span %s as evidence: %s", span.name, exc)

    def shutdown(self) -> None:
        with self._lock:
            self._open.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def _price(self, provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
        if self._pricer is None:
            return 0.0
        try:
            amount = float(self._pricer(provider, model, tokens_in, tokens_out))
        except Exception as exc:  # a pricing table error must not lose the evidence
            log.warning("warrant: pricer failed for %s/%s, recording cost 0: %s", provider, model, exc)
            return 0.0
        return amount if amount >= 0 else 0.0


def classify(attrs: Mapping[str, Any]) -> Optional[str]:
    """``"model_call"``, ``"tool_call"`` or ``None`` for spans that are not generative-AI spans."""
    op = attrs.get("gen_ai.operation.name")
    if op in _TOOL_OPERATIONS or "gen_ai.tool.name" in attrs:
        return "tool_call"
    if op in _MODEL_OPERATIONS or "gen_ai.request.model" in attrs or "gen_ai.response.model" in attrs:
        return "model_call"
    return None


def _first(attrs: Mapping[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = attrs.get(key)
        if value is not None and value != "":
            return value
    return None


def _int(value: Any) -> int:
    try:
        n = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0
