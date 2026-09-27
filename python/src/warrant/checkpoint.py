"""Checkpoints, witnesses and anchoring: moving trust away from the issuer (ADR 6, level 3).

An issuer signs a checkpoint committing to its whole chain as an RFC 9162 Merkle root. A witness
holds no records: it keeps the last checkpoint it co-signed per chain and co-signs a new one only
with a consistency proof that the new tree extends the old. An issuer that rewrote history cannot
produce that proof, so a co-signed checkpoint is evidence the issuer's own key cannot forge alone.

Anchoring submits a root to OpenTimestamps calendars, which commit it to Bitcoin: a witness of last
resort, blockchain as notary rather than ledger. The proof starts **pending**; completing it
(``ots upgrade``) and checking it against Bitcoin (``ots verify``) are done with the OpenTimestamps
client and are not reimplemented here.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from warrant.hashing import canonical_json
from warrant.merkle import consistency_proof, inclusion_proof, leaves_from_hex, root, verify_consistency, verify_inclusion
from warrant.signing import CHECKPOINT_CONTEXT, Keyring, SigningError, SigningKey

log = logging.getLogger("warrant.checkpoint")

SPEC = "adr/0.2"
UNSIGNED_FIELDS = ("signature", "cosignatures", "anchors", "consistency")
DEFAULT_CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
)
OTS_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
OTS_SHA256 = b"\x08"


class CheckpointError(ValueError):
    """A checkpoint is malformed, does not match the records, or a signature or proof fails."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def signed_message(checkpoint: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in checkpoint.items() if k not in UNSIGNED_FIELDS}
    return CHECKPOINT_CONTEXT + canonical_json(body).encode("utf-8")


def _chain(records: Iterable[Mapping[str, Any]], tenant: str, stream: str) -> List[Mapping[str, Any]]:
    chain = [r for r in records if r.get("tenant") == tenant and r.get("stream") == stream]
    chain.sort(key=lambda r: r.get("sequence") or 0)
    for expected, record in enumerate(chain, start=1):
        if record.get("sequence") != expected:
            raise CheckpointError(f"{tenant}/{stream} is not contiguous at sequence {expected}")
    return chain


def _leaves(chain: Sequence[Mapping[str, Any]]) -> List[bytes]:
    return leaves_from_hex([(r.get("seal") or {}).get("hash", "") for r in chain])


def create(
    records: Iterable[Mapping[str, Any]],
    key: SigningKey,
    *,
    tenant: str,
    stream: str,
    previous: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """An issuer-signed checkpoint over the whole chain. With ``previous``, embeds the consistency
    proof a witness that co-signed ``previous`` will ask for."""
    chain = _chain(records, tenant, stream)
    if not chain:
        raise CheckpointError(f"no records for {tenant}/{stream}")
    leaves = _leaves(chain)
    checkpoint: Dict[str, Any] = {
        "spec": SPEC,
        "kind": "checkpoint",
        "tenant": tenant,
        "stream": stream,
        "tree_size": len(leaves),
        "root": root(leaves).hex(),
        "head_hash": chain[-1]["seal"]["hash"],
        "issued_at": _now(),
        "issuer": key.issuer,
        "issuer_key_id": key.key_id,
    }
    checkpoint["signature"] = key.sign(signed_message(checkpoint))
    checkpoint["cosignatures"] = []
    checkpoint["anchors"] = []
    if previous is not None:
        if (previous.get("tenant"), previous.get("stream")) != (tenant, stream):
            raise CheckpointError("the previous checkpoint is for a different chain")
        old_size = int(previous.get("tree_size", 0))
        if not 0 < old_size <= len(leaves):
            raise CheckpointError(f"the previous checkpoint has size {old_size}; the chain now has {len(leaves)}")
        if root(leaves[:old_size]).hex() != previous.get("root"):
            raise CheckpointError("the chain no longer produces the previous checkpoint's root; history changed")
        checkpoint["consistency"] = {
            "from_size": old_size,
            "from_root": previous["root"],
            "proof": [h.hex() for h in consistency_proof(old_size, leaves)],
        }
    log.info("warrant checkpoint %s/%s size %d root %s", tenant, stream, len(leaves), checkpoint["root"][:12])
    return checkpoint


@dataclass
class CheckpointResult:
    tree_size: int
    issuer: str
    witnesses: List[str] = field(default_factory=list)
    independent_witnesses: List[str] = field(default_factory=list)


def _verify_signature(keyring: Keyring, key_id: Any, message: bytes, signature: Any, at: str, what: str) -> str:
    key = keyring.get(key_id) if isinstance(key_id, str) else None
    if key is None:
        raise CheckpointError(f"{what} key {key_id} is not in the key set")
    try:
        valid, why = key.valid_at(at)
    except SigningError as exc:
        raise CheckpointError(f"{what}: {exc}") from exc
    if not valid:
        raise CheckpointError(f"{what}: {why}")
    if not isinstance(signature, str) or not key.verify(message, signature):
        raise CheckpointError(f"{what} signature does not verify")
    return key.issuer


def verify_checkpoint(
    checkpoint: Mapping[str, Any],
    keyring: Keyring,
    *,
    records: Optional[Iterable[Mapping[str, Any]]] = None,
) -> CheckpointResult:
    """Check the issuer signature, every co-signature and, given the records, the root itself."""
    if checkpoint.get("kind") != "checkpoint" or checkpoint.get("spec") != SPEC:
        raise CheckpointError("not an adr/0.2 checkpoint")
    for name in ("tenant", "stream", "root", "head_hash", "issued_at", "issuer_key_id"):
        if not isinstance(checkpoint.get(name), str):
            raise CheckpointError(f"checkpoint has no {name}")
    size = checkpoint.get("tree_size")
    if not isinstance(size, int) or size < 1:
        raise CheckpointError("checkpoint tree_size must be a positive integer")
    message = signed_message(checkpoint)
    issuer = _verify_signature(keyring, checkpoint["issuer_key_id"], message, checkpoint.get("signature"), checkpoint["issued_at"], "issuer")
    if checkpoint.get("issuer") not in (None, issuer):
        raise CheckpointError(f"checkpoint names issuer {checkpoint.get('issuer')} but is signed by {issuer}")
    result = CheckpointResult(size, issuer)
    for co in checkpoint.get("cosignatures") or []:
        witness = _verify_signature(keyring, co.get("key_id"), message, co.get("signature"), co.get("at", ""), f"witness {co.get('witness')}")
        result.witnesses.append(witness)
        if witness != issuer:
            result.independent_witnesses.append(witness)
    consistency = checkpoint.get("consistency")
    if consistency:
        proof = [bytes.fromhex(h) for h in consistency.get("proof", [])]
        if not verify_consistency(int(consistency.get("from_size", 0)), size, bytes.fromhex(consistency.get("from_root", "")), bytes.fromhex(checkpoint["root"]), proof):
            raise CheckpointError("the embedded consistency proof does not verify")
    if records is not None:
        chain = _chain(records, checkpoint["tenant"], checkpoint["stream"])
        if len(chain) < size:
            raise CheckpointError(f"the checkpoint covers {size} records but only {len(chain)} were supplied")
        leaves = _leaves(chain[:size])
        if root(leaves).hex() != checkpoint["root"]:
            raise CheckpointError("the records do not produce the checkpoint's root")
        if chain[size - 1]["seal"]["hash"] != checkpoint["head_hash"]:
            raise CheckpointError("the record at the checkpoint's size is not its head")
    return result


def cosign(
    checkpoint: Dict[str, Any],
    witness_key: SigningKey,
    keyring: Keyring,
    *,
    last_seen: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Co-sign as a witness. ``last_seen`` is the witness's own previous checkpoint for this chain.

    Refuses a checkpoint that is smaller than, or not provably consistent with, what the witness
    already co-signed: the only thing a witness that holds no records can check, and the thing an
    issuer that rewrote history cannot fake.
    """
    verify_checkpoint(checkpoint, keyring)
    if witness_key.key_id == checkpoint.get("issuer_key_id"):
        raise CheckpointError("an issuer cannot witness its own checkpoint")
    if last_seen is not None:
        if (last_seen.get("tenant"), last_seen.get("stream")) != (checkpoint["tenant"], checkpoint["stream"]):
            raise CheckpointError("last_seen is for a different chain")
        old_size, new_size = int(last_seen["tree_size"]), int(checkpoint["tree_size"])
        if new_size < old_size:
            raise CheckpointError(f"the chain shrank from {old_size} to {new_size} records")
        if new_size == old_size:
            if last_seen["root"] != checkpoint["root"]:
                raise CheckpointError("same size, different root: the issuer rewrote history")
        else:
            consistency = checkpoint.get("consistency") or {}
            if consistency.get("from_size") != old_size or consistency.get("from_root") != last_seen["root"]:
                raise CheckpointError(f"no consistency proof from the {old_size}-record tree this witness last co-signed")
            proof = [bytes.fromhex(h) for h in consistency.get("proof", [])]
            if not verify_consistency(old_size, new_size, bytes.fromhex(last_seen["root"]), bytes.fromhex(checkpoint["root"]), proof):
                raise CheckpointError("the new checkpoint is not consistent with the last one; the issuer rewrote history")
    at = _now()
    co = {"witness": witness_key.issuer, "key_id": witness_key.key_id, "at": at, "signature": witness_key.sign(signed_message(checkpoint))}
    out = dict(checkpoint)
    out["cosignatures"] = list(checkpoint.get("cosignatures") or []) + [co]
    log.info("warrant witness %s co-signed %s/%s at size %s", witness_key.issuer, checkpoint["tenant"], checkpoint["stream"], checkpoint["tree_size"])
    return out


def prove_inclusion(records: Iterable[Mapping[str, Any]], checkpoint: Mapping[str, Any], record_id: str) -> Dict[str, Any]:
    """An inclusion proof for one record: shows it is in the checkpoint without revealing others."""
    chain = _chain(records, checkpoint["tenant"], checkpoint["stream"])[: int(checkpoint["tree_size"])]
    index = next((i for i, r in enumerate(chain) if r.get("record_id") == record_id), None)
    if index is None:
        raise CheckpointError(f"record {record_id} is not within the checkpoint's first {checkpoint['tree_size']} records")
    leaves = _leaves(chain)
    if root(leaves).hex() != checkpoint["root"]:
        raise CheckpointError("the records do not produce the checkpoint's root")
    return {
        "spec": SPEC,
        "kind": "inclusion",
        "record_id": record_id,
        "record_hash": chain[index]["seal"]["hash"],
        "index": index,
        "tree_size": len(leaves),
        "root": checkpoint["root"],
        "proof": [h.hex() for h in inclusion_proof(index, leaves)],
    }


def check_inclusion(proof: Mapping[str, Any], record: Optional[Mapping[str, Any]] = None) -> bool:
    """Verify an inclusion proof, optionally against the record it is about."""
    if record is not None and (record.get("seal") or {}).get("hash") != proof.get("record_hash"):
        return False
    try:
        return verify_inclusion(
            bytes.fromhex(proof["record_hash"]), int(proof["index"]), int(proof["tree_size"]),
            [bytes.fromhex(h) for h in proof["proof"]], bytes.fromhex(proof["root"]),
        )
    except (KeyError, ValueError, TypeError):
        return False


# -- OpenTimestamps -----------------------------------------------------------------

Post = Callable[[str, bytes], bytes]


def _http_post(url: str, body: bytes) -> bytes:
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Accept": "application/vnd.opentimestamps.v1", "User-Agent": "warrant-checkpoint"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https calendars
        return response.read(10_000)


def ots_file(digest: bytes, timestamp: bytes) -> bytes:
    """A detached ``.ots`` proof for ``digest``: magic, version 1, SHA-256 file op, digest, timestamp."""
    return OTS_MAGIC + b"\x01" + OTS_SHA256 + digest + timestamp


def anchor(
    checkpoint: Mapping[str, Any],
    out_dir: Path,
    *,
    calendars: Sequence[str] = DEFAULT_CALENDARS,
    post: Post = _http_post,
) -> Dict[str, Any]:
    """Submit the checkpoint root to OpenTimestamps calendars; write one pending ``.ots`` per calendar.

    Returns the checkpoint with an ``anchors`` entry per calendar that answered. Raises if none did.
    """
    digest = bytes.fromhex(checkpoint["root"])
    out_dir.mkdir(parents=True, exist_ok=True)
    anchors = list(checkpoint.get("anchors") or [])
    failures = []
    for calendar in calendars:
        host = calendar.split("//", 1)[-1].split("/", 1)[0]
        try:
            timestamp = post(calendar.rstrip("/") + "/digest", digest)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            failures.append(f"{host}: {type(exc).__name__}")
            log.warning("warrant anchor: calendar %s unavailable (%s)", host, type(exc).__name__)
            continue
        if not timestamp:
            failures.append(f"{host}: empty response")
            continue
        path = out_dir / f"{checkpoint['tenant']}.{checkpoint['stream']}.{checkpoint['tree_size']}.{host}.ots"
        path.write_bytes(ots_file(digest, timestamp))
        anchors.append({"type": "opentimestamps", "calendar": calendar, "digest": checkpoint["root"],
                        "status": "pending", "proof": base64.b64encode(timestamp).decode("ascii"), "file": path.name})
    if len(anchors) == len(checkpoint.get("anchors") or []):
        raise CheckpointError("no calendar accepted the root: " + "; ".join(failures))
    out = dict(checkpoint)
    out["anchors"] = anchors
    return out


def load(path: Path) -> Dict[str, Any]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CheckpointError(f"{path}: not JSON ({exc.msg})") from exc
    if not isinstance(body, dict):
        raise CheckpointError(f"{path}: not a checkpoint")
    return body


def save(checkpoint: Mapping[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
