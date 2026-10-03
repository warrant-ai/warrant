"""Canonical JSON and SHA-256 helpers shared by the SDK, the store and the verifier.

The canonical form is RFC 8785 (the JSON Canonicalization Scheme): keys sorted by UTF-16 code
unit, no whitespace, non-ASCII kept as is, and numbers written as ECMAScript writes them. A
record's hash covers everything except its ``seal`` and is chained to the previous record's hash
in the same stream.

Records sealed before 0.9.0 were hashed over Python's own number formatting (``4.0``, ``1e-07``),
which no other language reproduces. They carry no ``seal.canon`` and are still verified under that
rule; records sealed since carry ``seal.canon: "jcs"``.
"""

from __future__ import annotations

import hashlib
import json
from json.encoder import encode_basestring
from typing import Any, Callable, Mapping, Optional

#: The canonical form new records are sealed under, written to ``seal.canon``.
CANON = "jcs"


def canonical_json(obj: Any) -> str:
    """RFC 8785 canonical JSON. Every SDK produces these exact bytes for the same value."""
    parts: list = []
    _emit(obj, parts.append)
    return "".join(parts)


def legacy_canonical_json(obj: Any) -> str:
    """The form used before 0.9.0: sorted keys, compact, Python's number formatting."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _emit(value: Any, write: Callable[[str], Any]) -> None:
    if value is None:
        write("null")
    elif value is True:
        write("true")
    elif value is False:
        write("false")
    elif isinstance(value, str):
        write(encode_basestring(value))
    elif isinstance(value, int):
        write(int.__repr__(value))
    elif isinstance(value, float):
        write(_number(value))
    elif isinstance(value, Mapping):
        items = [(_key(k), v) for k, v in value.items()]
        if all(k.isascii() for k, _ in items):
            items.sort(key=lambda kv: kv[0])
        else:
            # Code point order and UTF-16 order differ once a key leaves the Basic Multilingual Plane.
            items.sort(key=lambda kv: kv[0].encode("utf-16-be", "surrogatepass"))
        write("{")
        for i, (key, item) in enumerate(items):
            write("," if i else "")
            write(encode_basestring(key))
            write(":")
            _emit(item, write)
        write("}")
    elif isinstance(value, (list, tuple)):
        write("[")
        for i, item in enumerate(value):
            write("," if i else "")
            _emit(item, write)
        write("]")
    else:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return canonical_json(key)
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def _number(value: float) -> str:
    """A double as ECMAScript's Number::toString writes it, which is what RFC 8785 requires."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("NaN and Infinity have no canonical JSON form")
    if value == 0:
        return "0"
    text = float.__repr__(value)
    if "e" not in text:
        # Python leaves exponent notation for [1e-4, 1e16), inside ECMAScript's own plain range.
        return text[:-2] if text.endswith(".0") else text
    sign = "-" if text[0] == "-" else ""
    mantissa, exponent = text.lstrip("-").split("e")
    digits = mantissa.replace(".", "")
    point = int(exponent) + 1  # where the decimal point sits, counted from the first digit
    if 0 < point <= 21:
        body = digits + "0" * (point - len(digits)) if len(digits) <= point else f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        body = "0." + "0" * -point + digits
    else:
        shown = point - 1
        body = digits[0] + (f".{digits[1:]}" if len(digits) > 1 else "") + ("e+" if shown > 0 else "e-") + str(abs(shown))
    return sign + body


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_bytes(content: Any) -> bytes:
    """The bytes a content hash covers: bytes as is, strings as UTF-8, anything else as canonical JSON."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    try:
        return canonical_json(content).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError(f"evidence content must be bytes, str or JSON-serialisable, got {type(content).__name__}") from exc


def content_hash(content: Any) -> str:
    """Hash evidence content. Bytes are hashed as is, strings as UTF-8, anything else as canonical JSON."""
    return sha256_hex(content_bytes(content))


SALT_BYTES = 32


def salted_hash(content: Any, salt: bytes) -> str:
    """ADR 4: SHA-256(salt || content). A low-variety value cannot be recovered by trying every one."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be {SALT_BYTES} bytes")
    return sha256_hex(bytes(salt) + content_bytes(content))


def record_hash(record: Mapping[str, Any], prev_hash: Optional[str]) -> str:
    """Hash of the record body (all fields except ``seal``) chained to ``prev_hash``.

    The body is encoded as the record's own ``seal.canon`` says: ``jcs``, or the pre-0.9.0 form
    when the seal names none. A form this version does not know raises ``ValueError``.
    """
    seal = record.get("seal")
    canon = seal.get("canon") if isinstance(seal, Mapping) else None
    if canon == CANON:
        encode = canonical_json
    elif canon is None:
        encode = legacy_canonical_json
    else:
        raise ValueError(f"record is sealed under canonical form {canon!r}, which this version does not know")
    body = {k: v for k, v in record.items() if k != "seal"}
    material = encode(body) + "\n" + (prev_hash or "")
    return sha256_hex(material.encode("utf-8"))


def seal_matches(record: Mapping[str, Any]) -> bool:
    """Does the record still hash to its own seal? False for an unsealed record or an unknown form."""
    seal = record.get("seal")
    if not isinstance(seal, Mapping) or not seal.get("hash"):
        return False
    try:
        return record_hash(record, seal.get("prev_hash")) == seal["hash"]
    except ValueError:
        return False
