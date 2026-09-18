"""ULID generation without a dependency (48-bit millisecond time, 80-bit randomness)."""

from __future__ import annotations

import hashlib
import os
import re
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def ulid(now_ms: int | None = None) -> str:
    """Return a 26-character Crockford base32 ULID, lexically sortable by creation time."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    if ts < 0 or ts >= 1 << 48:
        raise ValueError(f"timestamp out of ULID range: {ts}")
    return _encode((ts << 80) | int.from_bytes(os.urandom(10), "big"))


def deterministic_ulid(ts_ms: int, key: str) -> str:
    """ULID whose random part is derived from ``key``, so the same input always yields the same id.

    Used where one event may be recorded more than once (an imported span, a retried delivery)
    and the store's duplicate check by record id must recognise it.
    """
    ts_ms = max(0, min(ts_ms, (1 << 48) - 1))
    rand = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:10], "big")
    return _encode((ts_ms << 80) | rand)


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
