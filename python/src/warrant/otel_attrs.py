"""Attribute helpers for OpenTelemetry generative-AI semantic conventions, shared by the
live span processor and the importer. No OpenTelemetry dependency."""

from __future__ import annotations

from typing import Any, Mapping, Optional

MODEL_OPERATIONS = ("chat", "text_completion", "generate_content", "embeddings")
TOOL_OPERATIONS = ("execute_tool",)


def classify_attrs(attrs: Mapping[str, Any]) -> Optional[str]:
    """``"model_call"``, ``"tool_call"`` or ``None`` for spans that are not generative-AI spans."""
    op = attrs.get("gen_ai.operation.name")
    if op in TOOL_OPERATIONS or "gen_ai.tool.name" in attrs:
        return "tool_call"
    if op in MODEL_OPERATIONS or "gen_ai.request.model" in attrs or "gen_ai.response.model" in attrs:
        return "model_call"
    return None


def first_attr(attrs: Mapping[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = attrs.get(key)
        if value is not None and value != "":
            return value
    return None


def to_int(value: Any) -> int:
    try:
        n = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0
