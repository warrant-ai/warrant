"""RFC 9162 Merkle trees over a chain's record hashes: roots, inclusion and consistency proofs.

Leaves are the records' ``seal.hash`` values, in sequence order, as raw 32 bytes. An **inclusion
proof** shows one record is in a checkpointed tree without revealing any other record. A
**consistency proof** shows a larger tree extends a smaller one, which is what lets a witness that
holds no records refuse to co-sign a checkpoint whose issuer rewrote history (ADR 6).

Pure functions, standard library only; the same algorithms as Certificate Transparency.
"""

from __future__ import annotations

import hashlib
from typing import List, Sequence

EMPTY_ROOT = hashlib.sha256(b"").digest()


class ProofError(ValueError):
    """A proof is malformed, or asks about sizes or indices outside the tree."""


def leaf_hash(entry: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + entry).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """Largest power of two strictly less than ``n`` (n >= 2)."""
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def leaves_from_hex(hashes: Sequence[str]) -> List[bytes]:
    out = []
    for i, value in enumerate(hashes):
        try:
            raw = bytes.fromhex(value)
        except (ValueError, TypeError) as exc:
            raise ProofError(f"leaf {i} is not hex") from exc
        if len(raw) != 32:
            raise ProofError(f"leaf {i} is not a 32-byte hash")
        out.append(raw)
    return out


def root(entries: Sequence[bytes]) -> bytes:
    """MTH(D[n])."""
    n = len(entries)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaf_hash(entries[0])
    k = _split(n)
    return node_hash(root(entries[:k]), root(entries[k:]))


def inclusion_proof(index: int, entries: Sequence[bytes]) -> List[bytes]:
    """PATH(m, D[n]): the audit path for the entry at ``index``."""
    n = len(entries)
    if not 0 <= index < n:
        raise ProofError(f"index {index} is outside a tree of size {n}")
    if n == 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_proof(index, entries[:k]) + [root(entries[k:])]
    return inclusion_proof(index - k, entries[k:]) + [root(entries[:k])]


def verify_inclusion(entry: bytes, index: int, tree_size: int, proof: Sequence[bytes], expected_root: bytes) -> bool:
    """RFC 9162 section 2.1.3.2."""
    if not 0 <= index < tree_size:
        return False
    fn, sn = index, tree_size - 1
    r = leaf_hash(entry)
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == expected_root


def consistency_proof(old_size: int, entries: Sequence[bytes]) -> List[bytes]:
    """PROOF(m, D[n]): shows the tree of the first ``old_size`` entries is a prefix of this one."""
    n = len(entries)
    if not 0 < old_size <= n:
        raise ProofError(f"old size {old_size} must be between 1 and {n}")
    return _subproof(old_size, entries, True)


def _subproof(m: int, entries: Sequence[bytes], complete: bool) -> List[bytes]:
    n = len(entries)
    if m == n:
        return [] if complete else [root(entries)]
    k = _split(n)
    if m <= k:
        return _subproof(m, entries[:k], complete) + [root(entries[k:])]
    return _subproof(m - k, entries[k:], False) + [root(entries[:k])]


def verify_consistency(old_size: int, new_size: int, old_root: bytes, new_root: bytes, proof: Sequence[bytes]) -> bool:
    """RFC 9162 section 2.1.4.2."""
    if old_size < 1 or old_size > new_size:
        return False
    if old_size == new_size:
        return not proof and old_root == new_root
    path = list(proof)
    if not path:
        return False
    if old_size & (old_size - 1) == 0:  # exact power of two
        path.insert(0, old_root)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == old_root and sr == new_root and sn == 0
