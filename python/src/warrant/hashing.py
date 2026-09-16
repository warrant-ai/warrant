"""Canonical JSON and SHA-256 helpers shared by the SDK, the store and the verifier.

The canonical form is JSON with keys sorted, no whitespace, and non-ASCII kept as
is. A record's hash covers everything except its ``seal`` and is chained to the
previous record's hash in the same stream.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(content: Any) -> str:
    """Hash evidence content. Bytes are hashed as is, strings as UTF-8, anything else as canonical JSON."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        return sha256_hex(bytes(content))
    if isinstance(content, str):
        return sha256_hex(content.encode("utf-8"))
    try:
        return sha256_hex(canonical_json(content).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"evidence content must be bytes, str or JSON-serialisable, got {type(content).__name__}") from exc


def record_hash(record: Mapping[str, Any], prev_hash: Optional[str]) -> str:
    """Hash of the record body (all fields except ``seal``) chained to ``prev_hash``."""
    body = {k: v for k, v in record.items() if k != "seal"}
    material = canonical_json(body) + "\n" + (prev_hash or "")
    return sha256_hex(material.encode("utf-8"))
