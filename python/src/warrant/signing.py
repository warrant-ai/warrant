"""Issuer signing: Ed25519 over a sealed record's hash, and the key sets verifiers trust (ADR 5).

The store seals and, when given a key, signs. The signature covers ``"adr/0.2 seal\\n" + seal.hash``
and sits outside the hashed body, so a record verifies with or without it and signing a chain never
changes a hash. Keys are raw Ed25519, base64 in JSON files; the private file is written 0600.

What a signature proves, and what it does not: it binds the record to the holder of the issuer's key
and shows the record has not changed since. It does not stop the issuer rewriting its own history
before anyone else saw it, because the issuer holds its own key. Witnessed checkpoints
(:mod:`warrant.checkpoint`) exist for that.

Needs the ``sign`` extra: ``pip install "warrantai[sign]"``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

log = logging.getLogger("warrant.signing")

SEAL_CONTEXT = b"adr/0.2 seal\n"
CHECKPOINT_CONTEXT = b"adr/0.2 checkpoint\n"
KEY_ID_RE = re.compile(r"^ed25519:[a-f0-9]{16}$")


class SigningError(ValueError):
    """A key file is malformed, or a signature cannot be produced or checked."""


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:
        raise ImportError('signing needs the cryptography package: pip install "warrantai[sign]"') from exc
    return Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def _raw_public(public_key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id_for(raw_public: bytes) -> str:
    """``ed25519:`` and the first 16 hex characters of SHA-256 over the raw public key."""
    return "ed25519:" + hashlib.sha256(raw_public).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _b64decode(value: Any, what: str, length: int) -> bytes:
    if not isinstance(value, str):
        raise SigningError(f"{what} must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningError(f"{what} is not valid base64") from exc
    if len(raw) != length:
        raise SigningError(f"{what} must decode to {length} bytes, got {len(raw)}")
    return raw


@dataclass(frozen=True)
class PublicKey:
    """One entry of an issuer's published key set."""

    issuer: str
    key_id: str
    raw: bytes
    not_before: Optional[str] = None
    revoked_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issuer": self.issuer,
            "key_id": self.key_id,
            "alg": "Ed25519",
            "public_key": base64.b64encode(self.raw).decode("ascii"),
            "not_before": self.not_before,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, entry: Mapping[str, Any]) -> "PublicKey":
        if not isinstance(entry, Mapping):
            raise SigningError("a key entry must be an object")
        if entry.get("alg", "Ed25519") != "Ed25519":
            raise SigningError(f"unsupported key algorithm {entry.get('alg')!r}; ADR 0.2 uses Ed25519")
        issuer = entry.get("issuer")
        if not isinstance(issuer, str) or not issuer:
            raise SigningError("a key entry needs an issuer")
        raw = _b64decode(entry.get("public_key"), "public_key", 32)
        key_id = key_id_for(raw)
        if entry.get("key_id") not in (None, key_id):
            raise SigningError(f"key_id {entry.get('key_id')!r} does not match its public key ({key_id})")
        for field in ("not_before", "revoked_at"):
            value = entry.get(field)
            if value is not None:
                _parse_ts(value, field)
        return cls(issuer, key_id, raw, entry.get("not_before"), entry.get("revoked_at"))

    def valid_at(self, timestamp: str) -> Tuple[bool, str]:
        """Was this key allowed to sign a record stamped ``timestamp``?"""
        at = _parse_ts(timestamp, "record timestamp")
        if self.not_before and at < _parse_ts(self.not_before, "not_before"):
            return False, f"key {self.key_id} was not yet valid at {timestamp}"
        if self.revoked_at and at >= _parse_ts(self.revoked_at, "revoked_at"):
            return False, f"key {self.key_id} was revoked at {self.revoked_at}"
        return True, ""

    def verify(self, message: bytes, signature_b64: str) -> bool:
        _, Ed25519PublicKey, InvalidSignature = _crypto()
        try:
            signature = _b64decode(signature_b64, "signature", 64)
        except SigningError:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(self.raw).verify(signature, message)
            return True
        except InvalidSignature:
            return False


def _parse_ts(value: Any, what: str) -> datetime:
    if not isinstance(value, str):
        raise SigningError(f"{what} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SigningError(f"{what} is not ISO 8601: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SigningKey:
    """An issuer's private Ed25519 key. Hold it where the store or collector runs, nowhere else."""

    def __init__(self, issuer: str, private_raw: bytes, *, not_before: Optional[str] = None) -> None:
        if not isinstance(issuer, str) or not issuer:
            raise SigningError("issuer must be a non-empty string")
        if len(private_raw) != 32:
            raise SigningError("an Ed25519 private key is 32 bytes")
        Ed25519PrivateKey, _, _ = _crypto()
        self.issuer = issuer
        self._key = Ed25519PrivateKey.from_private_bytes(private_raw)
        self._private_raw = private_raw
        self.public = PublicKey(issuer, key_id_for(_raw_public(self._key.public_key())), _raw_public(self._key.public_key()), not_before)

    @property
    def key_id(self) -> str:
        return self.public.key_id

    @classmethod
    def generate(cls, issuer: str) -> "SigningKey":
        return cls(issuer, os.urandom(32), not_before=_now())

    @classmethod
    def load(cls, path: Union[str, Path]) -> "SigningKey":
        path = Path(path)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise SigningError(f"{path}: not a readable key file ({type(exc).__name__})") from exc
        if not isinstance(body, dict) or "private_key" not in body:
            raise SigningError(f"{path}: not a private key file (no private_key)")
        if hasattr(os, "stat") and os.name == "posix" and path.stat().st_mode & 0o077:
            log.warning("warrant signing key %s is readable by other users; chmod 600 it", path)
        key = cls(body.get("issuer", ""), _b64decode(body["private_key"], "private_key", 32), not_before=body.get("not_before"))
        if body.get("key_id") not in (None, key.key_id):
            raise SigningError(f"{path}: key_id does not match the private key")
        return key

    def save(self, directory: Union[str, Path]) -> Tuple[Path, Path]:
        """Write ``<issuer>.key`` (0600, private) and ``<issuer>.keys.json`` (public key set)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        private_path = directory / f"{self.issuer}.key"
        if private_path.exists():
            raise FileExistsError(f"{private_path} exists; refusing to overwrite a signing key")
        body = {
            "issuer": self.issuer,
            "key_id": self.key_id,
            "alg": "Ed25519",
            "private_key": base64.b64encode(self._private_raw).decode("ascii"),
            "not_before": self.public.not_before,
        }
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.write("\n")
        public_path = directory / f"{self.issuer}.keys.json"
        Keyring([self.public]).save(public_path)
        return private_path, public_path

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._key.sign(message)).decode("ascii")

    def sign_seal(self, record_hash: str, sealed_at: Optional[str] = None) -> Dict[str, str]:
        """The ``seal`` fields a signing store adds: key_id and signature."""
        out = {"key_id": self.key_id, "signature": self.sign(seal_message(record_hash, sealed_at))}
        if sealed_at is not None:
            out["sealed_at"] = sealed_at
        return out


class Keyring:
    """The public keys a verifier trusts, from one or more issuers' published key sets."""

    def __init__(self, keys: Iterable[PublicKey] = ()) -> None:
        self._keys: Dict[str, PublicKey] = {}
        for key in keys:
            self.add(key)

    def add(self, key: PublicKey) -> None:
        existing = self._keys.get(key.key_id)
        if existing is not None and existing.issuer != key.issuer:
            raise SigningError(f"key {key.key_id} is claimed by two issuers: {existing.issuer} and {key.issuer}")
        # A revocation seen anywhere wins: a later key set that forgot it must not un-revoke.
        if existing is not None and existing.revoked_at and not key.revoked_at:
            return
        self._keys[key.key_id] = key

    def get(self, key_id: str) -> Optional[PublicKey]:
        return self._keys.get(key_id)

    def __len__(self) -> int:
        return len(self._keys)

    def __iter__(self):
        return iter(self._keys.values())

    def issuers(self) -> List[str]:
        return sorted({k.issuer for k in self._keys.values()})

    @classmethod
    def load(cls, *paths: Union[str, Path]) -> "Keyring":
        ring = cls()
        for path in paths:
            path = Path(path)
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SigningError(f"{path}: not JSON ({exc.msg})") from exc
            entries = body.get("keys") if isinstance(body, dict) else None
            if not isinstance(entries, list):
                raise SigningError(f"{path}: a key set is {{\"keys\": [...]}}")
            if any("private_key" in e for e in entries if isinstance(e, dict)):
                raise SigningError(f"{path}: contains a private key; publish only public keys")
            for entry in entries:
                ring.add(PublicKey.from_dict(entry))
        return ring

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps({"keys": [k.to_dict() for k in self._keys.values()]}, indent=2) + "\n", encoding="utf-8")


def seal_message(record_hash: str, sealed_at: Optional[str] = None) -> bytes:
    """What an issuer signs. With ``sealed_at`` (0.7.1 on), the sealing time is signed too.

    Key validity must be judged at the moment of sealing, not at the decision's timestamp: an
    imported record describes a past decision but is sealed today, and a key revoked last week must
    not be able to sign a record dated before its revocation. So the store stamps ``seal.sealed_at``
    and signs it. Records signed by 0.7.0 have no ``sealed_at`` and keep the original message.
    """
    message = SEAL_CONTEXT + record_hash.encode("ascii")
    if sealed_at is not None:
        message += b"\n" + sealed_at.encode("ascii")
    return message


def verify_seal(record: Mapping[str, Any], keyring: Keyring) -> Tuple[bool, str]:
    """Check a sealed record's issuer signature. Returns ``(ok, reason)``.

    Checks the signature against ``seal.hash`` only. Whether that hash matches the body is the
    chain verifier's job, and both are needed: a valid signature over a hash the body no longer
    produces proves nothing about the body.
    """
    seal = record.get("seal") or {}
    signature, key_id = seal.get("signature"), seal.get("key_id")
    if not signature:
        return False, "unsigned"
    if not key_id:
        return False, "signature without key_id"
    key = keyring.get(key_id)
    if key is None:
        return False, f"signed by unknown key {key_id}"
    try:
        sealed_at = seal.get("sealed_at")
        valid, why = key.valid_at(sealed_at or record.get("timestamp", ""))
    except SigningError as exc:
        return False, str(exc)
    if not valid:
        return False, why
    if not key.verify(seal_message(str(seal.get("hash", "")), seal.get("sealed_at")), signature):
        return False, f"signature does not verify under {key_id}"
    return True, key.issuer


def resolve_signing_key(value: Union[str, Path, SigningKey, None]) -> Optional[SigningKey]:
    """A ``SigningKey`` from an object, a path, or ``$WARRANT_SIGNING_KEY``; ``None`` if none is set."""
    if isinstance(value, SigningKey):
        return value
    target = value if value is not None else os.environ.get("WARRANT_SIGNING_KEY")
    if not target:
        return None
    return SigningKey.load(target)
