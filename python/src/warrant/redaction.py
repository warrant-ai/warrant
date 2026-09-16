"""Client-side redaction applied to a record before it leaves the process."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Pattern, Sequence, Union

# Free-text fields that regex patterns are applied to. Structural fields (ids,
# hashes, URIs, timestamps, class names) are never touched by patterns because a
# broad pattern would corrupt them; use ``fields`` to drop those outright.
TEXT_FIELDS: Sequence[str] = (
    "decision.summary",
    "decision.alternatives[]",
    "evidence[].excerpt",
    "human.note",
)


class Redactor:
    """Redact records with regex patterns over free-text fields and explicit field paths.

    ``patterns`` are regular expressions; every match inside the free-text fields is
    replaced with ``replacement``. ``fields`` are dotted paths such as
    ``"evidence[].excerpt"`` or ``"actor.on_behalf_of"``; a string value at that path is
    replaced with ``replacement`` and any other value is removed.
    """

    def __init__(
        self,
        patterns: Iterable[Union[str, Pattern[str]]] = (),
        fields: Iterable[str] = (),
        replacement: str = "[REDACTED]",
    ) -> None:
        self._patterns: List[Pattern[str]] = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        self._fields: List[List[str]] = [_parse_path(f) for f in fields]
        self._replacement = replacement

    def apply(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Return a redacted deep copy; the input is not modified."""
        out = copy.deepcopy(record)
        if self._patterns:
            for path in TEXT_FIELDS:
                _walk(out, _parse_path(path), self._scrub)
        for path in self._fields:
            _walk(out, path, lambda _v: self._replacement, remove_non_strings=True)
        return out

    def _scrub(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        for pattern in self._patterns:
            value = pattern.sub(self._replacement, value)
        return value


def _parse_path(path: str) -> List[str]:
    if not path or path.startswith(".") or path.endswith("."):
        raise ValueError(f"invalid field path: {path!r}")
    return path.split(".")


def _walk(node: Any, path: List[str], fn, remove_non_strings: bool = False) -> None:
    """Apply ``fn`` at ``path`` below ``node``. ``seg[]`` means every element of a list."""
    if not path or not isinstance(node, dict):
        return
    seg, rest = path[0], path[1:]
    is_list = seg.endswith("[]")
    key = seg[:-2] if is_list else seg
    if key not in node:
        return
    if is_list:
        items = node[key]
        if not isinstance(items, list):
            return
        if rest:
            for item in items:
                _walk(item, rest, fn, remove_non_strings)
        else:
            node[key] = [fn(v) if isinstance(v, str) or not remove_non_strings else None for v in items]
            node[key] = [v for v in node[key] if v is not None]
        return
    if rest:
        _walk(node[key], rest, fn, remove_non_strings)
        return
    value = node[key]
    if remove_non_strings and not isinstance(value, str):
        del node[key]
    else:
        node[key] = fn(value)
