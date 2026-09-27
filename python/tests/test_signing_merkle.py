"""Issuer signing, key sets, and RFC 9162 Merkle proofs."""

import hashlib
import json
import os
import stat

import pytest

pytest.importorskip("cryptography")

from warrant.hashing import content_hash, salted_hash
from warrant.merkle import (
    ProofError,
    consistency_proof,
    inclusion_proof,
    root,
    verify_consistency,
    verify_inclusion,
)
from warrant.signing import Keyring, PublicKey, SigningError, SigningKey, key_id_for, verify_seal
from warrant.store import SQLiteStore


def _record(i, **extra):
    record = {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D{i:04d}",
        "record_type": "decision",
        "tenant": "demo-bank",
        "stream": "lending",
        "timestamp": "2026-09-27T10:00:00.000Z",
        "schema_version": "0",
        "origin": "live",
        "actor": {"name": "credit-agent", "version": "1"},
        "decision": {"class": "credit.approve", "action": "approve", "subject": f"LN-{i}", "status": "acted"},
        "mandate": {"result": "allow"},
    }
    record.update(extra)
    return record


# -- keys ------------------------------------------------------------------------------------


def test_generated_key_round_trips_and_the_private_file_is_private(tmp_path):
    key = SigningKey.generate("demo-bank")
    private_path, public_path = key.save(tmp_path)
    assert stat.S_IMODE(os.stat(private_path).st_mode) == 0o600
    loaded = SigningKey.load(private_path)
    assert loaded.key_id == key.key_id
    assert key.key_id.startswith("ed25519:") and len(key.key_id) == len("ed25519:") + 16
    ring = Keyring.load(public_path)
    assert ring.get(key.key_id).issuer == "demo-bank"
    assert "private_key" not in public_path.read_text()


def test_saving_never_overwrites_a_signing_key(tmp_path):
    SigningKey.generate("demo-bank").save(tmp_path)
    with pytest.raises(FileExistsError):
        SigningKey.generate("demo-bank").save(tmp_path)


def test_a_key_set_that_contains_a_private_key_is_refused(tmp_path):
    key = SigningKey.generate("demo-bank")
    private_path, _ = key.save(tmp_path)
    leaked = tmp_path / "leak.json"
    leaked.write_text(json.dumps({"keys": [json.loads(private_path.read_text())]}))
    with pytest.raises(SigningError, match="private key"):
        Keyring.load(leaked)


def test_a_key_id_that_does_not_match_its_key_is_refused():
    key = SigningKey.generate("demo-bank")
    entry = key.public.to_dict()
    entry["key_id"] = "ed25519:0000000000000000"
    with pytest.raises(SigningError, match="does not match"):
        PublicKey.from_dict(entry)


def test_one_key_cannot_belong_to_two_issuers():
    key = SigningKey.generate("demo-bank")
    ring = Keyring([key.public])
    impostor = PublicKey("other-bank", key.key_id, key.public.raw)
    with pytest.raises(SigningError, match="two issuers"):
        ring.add(impostor)


def test_a_revocation_is_not_undone_by_a_later_key_set_that_forgot_it():
    key = SigningKey.generate("demo-bank")
    revoked = PublicKey(key.issuer, key.key_id, key.public.raw, None, "2026-09-01T00:00:00Z")
    ring = Keyring([revoked])
    ring.add(key.public)
    assert ring.get(key.key_id).revoked_at == "2026-09-01T00:00:00Z"


# -- signing at seal -------------------------------------------------------------------------


def test_a_signing_store_signs_every_record_and_the_signature_verifies(tmp_path):
    key = SigningKey.generate("demo-bank")
    store = SQLiteStore(tmp_path / "r.db", signer=key)
    store.write([_record(1), _record(2)])
    records = list(store.iter_records())
    store.close()
    ring = Keyring([key.public])
    for record in records:
        assert record["seal"]["key_id"] == key.key_id
        assert verify_seal(record, ring) == (True, "demo-bank")


def test_a_store_without_a_key_writes_what_it_always_wrote(tmp_path):
    store = SQLiteStore(tmp_path / "r.db")
    store.write([_record(1)])
    record = next(store.iter_records())
    store.close()
    assert set(record["seal"]) == {"prev_hash", "hash"}


def test_signature_failures_say_why(tmp_path):
    key, other = SigningKey.generate("demo-bank"), SigningKey.generate("other")
    store = SQLiteStore(tmp_path / "r.db", signer=key)
    store.write([_record(1)])
    record = next(store.iter_records())
    store.close()

    assert verify_seal({**record, "seal": {k: v for k, v in record["seal"].items() if k != "signature"}}, Keyring([key.public])) == (False, "unsigned")
    assert "unknown key" in verify_seal(record, Keyring([other.public]))[1]
    forged = {**record, "seal": {**record["seal"], "hash": "0" * 64}}
    assert "does not verify" in verify_seal(forged, Keyring([key.public]))[1]
    revoked = PublicKey(key.issuer, key.key_id, key.public.raw, None, "2026-01-01T00:00:00Z")
    assert "revoked" in verify_seal(record, Keyring([revoked]))[1]
    early = PublicKey(key.issuer, key.key_id, key.public.raw, "2027-01-01T00:00:00Z", None)
    assert "not yet valid" in verify_seal(record, Keyring([early]))[1]


def test_key_id_is_derived_from_the_public_key():
    raw = bytes(range(32))
    assert key_id_for(raw) == "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


# -- salted digests --------------------------------------------------------------------------


def test_a_salted_digest_cannot_be_matched_by_trying_the_obvious_values():
    salt = os.urandom(32)
    secret = {"hits": 0}
    digest = salted_hash(secret, salt)
    guesses = [content_hash({"hits": n}) for n in range(10)]
    assert digest not in guesses
    assert salted_hash(secret, salt) == digest
    assert salted_hash(secret, os.urandom(32)) != digest


def test_a_salt_must_be_32_bytes():
    with pytest.raises(ValueError):
        salted_hash("x", b"short")


# -- Merkle ----------------------------------------------------------------------------------

ENTRIES = [hashlib.sha256(bytes([i])).digest() for i in range(24)]


def test_root_matches_the_rfc_6962_reference_vector():
    leaves = [bytes.fromhex(x) for x in ["", "00", "10", "2021", "3031", "40414243", "5051525354555657", "606162636465666768696a6b6c6d6e6f"]]
    assert root(leaves).hex() == "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"
    assert root([]).hex() == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13, 24])
def test_every_inclusion_proof_verifies_and_rejects_the_wrong_entry(n):
    r = root(ENTRIES[:n])
    for i in range(n):
        proof = inclusion_proof(i, ENTRIES[:n])
        assert verify_inclusion(ENTRIES[i], i, n, proof, r)
        if n > 1:
            assert not verify_inclusion(ENTRIES[(i + 1) % n], i, n, proof, r)
    with pytest.raises(ProofError):
        inclusion_proof(n, ENTRIES[:n])


@pytest.mark.parametrize("n", [2, 3, 7, 8, 9, 24])
def test_consistency_proofs_hold_for_appends_and_fail_for_rewrites(n):
    new_root = root(ENTRIES[:n])
    for m in range(1, n):
        assert verify_consistency(m, n, root(ENTRIES[:m]), new_root, consistency_proof(m, ENTRIES[:n]))
        rewritten = list(ENTRIES[:n])
        rewritten[m - 1] = b"\x00" * 32
        assert not verify_consistency(m, n, root(ENTRIES[:m]), root(rewritten), consistency_proof(m, rewritten))
    assert verify_consistency(n, n, new_root, new_root, [])
    assert not verify_consistency(n, n, new_root, root(ENTRIES[:n - 1]), [])


def test_the_conformance_vectors_still_match_this_implementation():
    """conformance/adr-vectors.json is shared with the JS suite; the two must never drift."""
    import base64
    from pathlib import Path

    from warrant.hashing import record_hash
    from warrant.signing import SEAL_CONTEXT, SigningKey

    vectors = json.loads((Path(__file__).resolve().parents[2] / "conformance" / "adr-vectors.json").read_text())
    key = SigningKey(vectors["key"]["issuer"], bytes.fromhex(vectors["key"]["private_key_hex"]))
    assert key.key_id == vectors["key"]["key_id"]
    for v in vectors["salted"]:
        assert content_hash(v["content"]) == v["plain"]
        assert salted_hash(v["content"], bytes.fromhex(v["salt_hex"])) == v["salted"]
    seal = vectors["seal"]
    assert record_hash(seal["record"], None) == seal["hash"]
    assert key.sign(SEAL_CONTEXT + seal["hash"].encode()) == seal["signature"]
    leaves = [bytes.fromhex(e) for e in vectors["merkle"]["entries"]]
    for n, r in vectors["merkle"]["roots"].items():
        assert root(leaves[: int(n)]).hex() == r
    for v in vectors["merkle"]["inclusion"]:
        assert [p.hex() for p in inclusion_proof(v["index"], leaves[: v["size"]])] == v["proof"]
    for v in vectors["merkle"]["consistency"]:
        assert [p.hex() for p in consistency_proof(v["from"], leaves[: v["to"]])] == v["proof"]


# -- sealing time (0.7.1) ------------------------------------------------------------------------


def test_an_imported_record_of_a_past_decision_is_validly_signed_by_a_new_key(tmp_path):
    """Key validity is judged when the record was sealed, not when the decision it describes was made."""
    key = SigningKey.generate("demo-bank")  # not_before: now
    store = SQLiteStore(tmp_path / "r.db", signer=key)
    store.write([_record(1, timestamp="2025-01-15T10:00:00.000Z", origin="imported")])
    record = next(store.iter_records())
    store.close()
    assert record["seal"]["sealed_at"] > record["timestamp"]
    assert verify_seal(record, Keyring([key.public])) == (True, "demo-bank")


def test_a_key_revoked_before_sealing_fails_even_for_an_old_decision(tmp_path):
    key = SigningKey.generate("demo-bank")
    store = SQLiteStore(tmp_path / "r.db", signer=key)
    store.write([_record(1, timestamp="2025-01-15T10:00:00.000Z")])
    record = next(store.iter_records())
    store.close()
    revoked = PublicKey(key.issuer, key.key_id, key.public.raw, None, "2026-01-01T00:00:00Z")
    ok, why = verify_seal(record, Keyring([revoked]))
    assert not ok and "revoked" in why


def test_the_sealing_time_is_signed_so_it_cannot_be_moved(tmp_path):
    key = SigningKey.generate("demo-bank")
    store = SQLiteStore(tmp_path / "r.db", signer=key)
    store.write([_record(1)])
    record = next(store.iter_records())
    store.close()
    moved = {**record, "seal": {**record["seal"], "sealed_at": "2099-01-01T00:00:00.000Z"}}
    assert "does not verify" in verify_seal(moved, Keyring([key.public]))[1]


def test_a_0_7_0_signature_without_a_sealing_time_still_verifies():
    from warrant.hashing import record_hash
    from warrant.signing import SEAL_CONTEXT

    key = SigningKey("demo-bank", bytes(range(32)), not_before="2026-01-01T00:00:00Z")
    record = {**_record(1), "sequence": 1}
    h = record_hash(record, None)
    record["seal"] = {"prev_hash": None, "hash": h, "key_id": key.key_id, "signature": key.sign(SEAL_CONTEXT + h.encode())}
    assert verify_seal(record, Keyring([key.public]))[0]


def test_the_sealed_at_vector_matches():
    from pathlib import Path

    from warrant.signing import seal_message

    vectors = json.loads((Path(__file__).resolve().parents[2] / "conformance" / "adr-vectors.json").read_text())
    key = SigningKey(vectors["key"]["issuer"], bytes.fromhex(vectors["key"]["private_key_hex"]))
    v = vectors["seal_sealed_at"]
    assert seal_message(v["hash"], v["sealed_at"]).decode() == v["message"]
    assert key.sign(seal_message(v["hash"], v["sealed_at"])) == v["signature"]
