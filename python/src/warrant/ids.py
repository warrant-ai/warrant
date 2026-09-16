"""ULID generation without a dependency (48-bit millisecond time, 80-bit randomness)."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None) -> str:
    """Return a 26-character Crockford base32 ULID, lexically sortable by creation time."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    if ts < 0 or ts >= 1 << 48:
        raise ValueError(f"timestamp out of ULID range: {ts}")
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
