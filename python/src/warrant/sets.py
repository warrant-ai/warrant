"""Decision sets: named collections of real recorded decisions, saved as portable JSONL.

A set file starts with a header line and then holds one item per line::

    {"set": "lending-edge", "created_at": "...", "source": {...}, "count": 200}
    {"record": {...decision record, outcome joined...}, "evidence_content": {"<hash>": {...}}}

``evidence_content`` carries the tool results captured with ``capture_evidence`` so
frozen replay can serve them without touching the tools again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from warrant.store import SQLiteStore, resolve_tenant

log = logging.getLogger("warrant.sets")


@dataclass
class SetItem:
    record: Dict[str, Any]
    evidence_content: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.record["record_id"]

    @property
    def subject(self) -> str:
        return self.record["decision"]["subject"]

    @property
    def outcome_label(self) -> Optional[str]:
        return (self.record.get("outcome") or {}).get("label")


@dataclass
class DecisionSet:
    name: str
    items: List[SetItem]
    created_at: str = ""
    source: Dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[SetItem]:
        return iter(self.items)

    def save(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            header = {"set": self.name, "created_at": self.created_at or _now(), "source": self.source, "count": len(self.items)}
            fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            for item in self.items:
                fh.write(json.dumps({"record": item.record, "evidence_content": item.evidence_content}, ensure_ascii=False) + "\n")
        return path

    @classmethod
    def load(cls, path: Union[str, Path]) -> "DecisionSet":
        path = Path(path)
        with open(path, "r", encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        if not lines:
            raise ValueError(f"{path}: empty set file")
        try:
            header = json.loads(lines[0])
            if not isinstance(header, dict) or "set" not in header:
                raise ValueError("first line is not a set header")
            items = []
            for n, line in enumerate(lines[1:], start=2):
                raw = json.loads(line)
                if not isinstance(raw, dict) or not isinstance(raw.get("record"), dict):
                    raise ValueError(f"line {n}: not a set item")
                items.append(SetItem(record=raw["record"], evidence_content=raw.get("evidence_content") or {}))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON on line {exc.lineno}") from exc
        return cls(name=header["set"], items=items, created_at=header.get("created_at", ""), source=header.get("source") or {})


def build_set(
    store: SQLiteStore,
    name: str,
    *,
    stream: str,
    tenant: Optional[str] = None,
    where: Optional[str] = None,
    limit: Optional[int] = None,
    subjects: Optional[List[str]] = None,
) -> DecisionSet:
    """Select decision records from a store, join each one's latest outcome, and attach captured evidence.

    ``where`` is a CEL expression over the joined record, e.g. ``outcome.label == 'default'``
    or ``mandate.result == 'deny' && cost.amount > 3``; it needs the ``policy`` extra.
    """
    predicate = _compile_where(where) if where else None
    wanted = set(subjects) if subjects else None
    items: List[SetItem] = []
    for record in store.iter_records(stream, resolve_tenant(store, stream, tenant)):
        if record.get("record_type") != "decision":
            continue
        if wanted is not None and record["decision"]["subject"] not in wanted:
            continue
        outcome = store.latest_outcome(record["record_id"])
        if outcome is not None:
            record = dict(record, outcome=outcome["outcome"])
        if predicate is not None and not predicate(record):
            continue
        content: Dict[str, Dict[str, Any]] = {}
        for ev in record.get("evidence") or []:
            blob = store.get_blob(ev["content_hash"])
            if blob is not None:
                content[ev["content_hash"]] = blob
        items.append(SetItem(record=record, evidence_content=content))
        if limit is not None and len(items) >= limit:
            break
    log.info("warrant set %s: %d decision(s) selected from stream %s", name, len(items), stream)
    return DecisionSet(name=name, items=items, created_at=_now(), source={"store": str(store.path), "stream": stream, "where": where, "limit": limit})


def _compile_where(expression: str):
    try:
        import celpy
    except ImportError as exc:
        raise ImportError('--where needs the cel-python package: pip install "warrantai[policy]"') from exc
    env = celpy.Environment()
    try:
        program = env.program(env.compile(expression))
    except celpy.CELParseError as exc:
        raise ValueError(f"invalid --where expression: {exc}") from exc

    def predicate(record: Dict[str, Any]) -> bool:
        try:
            return bool(program.evaluate(celpy.json_to_cel(record)))
        except celpy.CELEvalError:
            return False  # a record lacking a referenced field simply does not match

    return predicate


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
