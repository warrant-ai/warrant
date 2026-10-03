"""``warrant`` subcommands for the Agent Decision Record: keys, checkpoints, trace, evidence, erase."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} line {lineno}: {exc.msg}") from exc
    return out


def _keyring(paths: Optional[List[str]]):
    if not paths:
        return None
    from warrant.signing import Keyring

    return Keyring.load(*paths)


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


# -- keys -------------------------------------------------------------------------------


def cmd_keys_generate(args: argparse.Namespace) -> int:
    try:
        from warrant.signing import SigningKey

        key = SigningKey.generate(args.issuer)
        private_path, public_path = key.save(args.out)
    except (ImportError, FileExistsError, ValueError) as exc:
        return _fail(str(exc))
    print(f"issuer {key.issuer}, key {key.key_id}")
    print(f"  private key: {private_path} (0600; keep it where records are sealed, nowhere else)")
    print(f"  public key set: {public_path} (publish this; verifiers need it)")
    return 0


def cmd_keys_show(args: argparse.Namespace) -> int:
    try:
        from warrant.signing import Keyring, SigningError, SigningKey

        try:
            key = SigningKey.load(args.file)
            ring = Keyring([key.public])
        except SigningError:
            ring = Keyring.load(args.file)
    except (ImportError, FileNotFoundError, ValueError) as exc:
        return _fail(str(exc))
    for key in ring:
        status = f"revoked {key.revoked_at}" if key.revoked_at else "active"
        print(f"{key.issuer} {key.key_id} not_before={key.not_before or '-'} {status}")
    if args.public:
        json.dump({"keys": [k.to_dict() for k in ring]}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


def cmd_keys_revoke(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from warrant.signing import Keyring, PublicKey, SigningError

    try:
        ring = Keyring.load(args.keyset)
    except (FileNotFoundError, SigningError) as exc:
        return _fail(str(exc))
    target = ring.get(args.key_id)
    if target is None:
        return _fail(f"{args.key_id} is not in {args.keyset}")
    at = args.at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = Keyring([k if k.key_id != args.key_id else PublicKey(k.issuer, k.key_id, k.raw, k.not_before, at) for k in ring])
    updated.save(args.keyset)
    print(f"{args.key_id} revoked from {at}; records it signed after that time will fail verification. Publish {args.keyset} again.")
    return 0


# -- checkpoints ------------------------------------------------------------------------


def _store_records(store_url: str, stream: str, tenant: Optional[str]) -> List[Dict[str, Any]]:
    from warrant.store import open_store

    store = open_store(store_url, read_only=True)
    try:
        return list(store.iter_records(stream, tenant))
    finally:
        store.close()


def _records_from(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if getattr(args, "export", None):
        return _read_jsonl(args.export)
    return _store_records(args.store, args.stream, args.tenant)


def _single_tenant(records: Iterable[Mapping[str, Any]], tenant: Optional[str]) -> str:
    tenants = sorted({r.get("tenant") for r in records})
    if tenant:
        return tenant
    if len(tenants) != 1:
        raise ValueError(f"the stream holds several tenants ({', '.join(map(str, tenants))}); pass --tenant")
    return tenants[0]


def cmd_checkpoint_create(args: argparse.Namespace) -> int:
    from warrant import checkpoint as cp
    from warrant.signing import SigningError, SigningKey

    try:
        key = SigningKey.load(args.key)
        records = _records_from(args)
        tenant = _single_tenant(records, args.tenant)
        previous = cp.load(Path(args.previous)) if args.previous else None
        out = cp.create(records, key, tenant=tenant, stream=args.stream, previous=previous)
    except (ImportError, FileNotFoundError, ValueError, SigningError) as exc:
        return _fail(f"checkpoint not created: {exc}")
    cp.save(out, Path(args.output))
    print(f"checkpoint {tenant}/{args.stream}: {out['tree_size']} record(s), root {out['root'][:16]}..., signed by {key.key_id}")
    if previous is not None:
        print(f"  carries a consistency proof from size {previous['tree_size']}")
    print(f"  written to {args.output}")
    return 0


def _witness_state(state_dir: Optional[str], checkpoint: Mapping[str, Any]) -> Optional[Path]:
    if not state_dir:
        return None
    safe = f"{checkpoint['tenant']}__{checkpoint['stream']}".replace("/", "_")
    return Path(state_dir) / f"{safe}.last.json"


def cmd_checkpoint_cosign(args: argparse.Namespace) -> int:
    from warrant import checkpoint as cp
    from warrant.signing import SigningError, SigningKey

    try:
        checkpoint = cp.load(Path(args.checkpoint))
        key = SigningKey.load(args.key)
        ring = _keyring(args.keys)
        if ring is None:
            return _fail("pass --keys with the issuer's public key set", 2)
        state_path = _witness_state(args.state, checkpoint)
        last_seen = cp.load(state_path) if state_path and state_path.exists() else None
        out = cp.cosign(checkpoint, key, ring, last_seen=last_seen)
    except (ImportError, FileNotFoundError, ValueError, SigningError) as exc:
        return _fail(f"not co-signed: {exc}")
    cp.save(out, Path(args.output or args.checkpoint))
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        cp.save({k: v for k, v in out.items() if k not in ("anchors",)}, state_path)
    print(f"witness {key.issuer} co-signed {checkpoint['tenant']}/{checkpoint['stream']} at size {checkpoint['tree_size']}")
    if last_seen is None:
        print("  first checkpoint this witness has seen for the chain; later ones must prove consistency with it")
    return 0


def cmd_checkpoint_verify(args: argparse.Namespace) -> int:
    from warrant import checkpoint as cp

    try:
        checkpoint = cp.load(Path(args.checkpoint))
        ring = _keyring(args.keys)
        if ring is None:
            return _fail("pass --keys with the issuer's and witnesses' public key sets", 2)
        records = _read_jsonl(args.export) if args.export else None
        result = cp.verify_checkpoint(checkpoint, ring, records=records)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(f"checkpoint FAILED: {exc}")
    print(f"checkpoint {checkpoint['tenant']}/{checkpoint['stream']} size {result.tree_size}: signed by {result.issuer}")
    print(f"  witnesses: {', '.join(result.witnesses) if result.witnesses else 'none'}")
    if records is not None:
        print("  root recomputed from the export: matches")
    for anchor in checkpoint.get("anchors") or []:
        print(f"  anchor {anchor.get('type')} via {anchor.get('calendar')}: {anchor.get('status')} ({anchor.get('file')}; complete with 'ots upgrade', check with 'ots verify')")
    return 0


def cmd_checkpoint_prove(args: argparse.Namespace) -> int:
    from warrant import checkpoint as cp

    try:
        checkpoint = cp.load(Path(args.checkpoint))
        records = _read_jsonl(args.export)
        proof = cp.prove_inclusion(records, checkpoint, args.record)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(f"no proof: {exc}")
    text = json.dumps(proof, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"inclusion proof for {args.record} ({len(proof['proof'])} hashes) written to {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_checkpoint_anchor(args: argparse.Namespace) -> int:
    from warrant import checkpoint as cp

    try:
        checkpoint = cp.load(Path(args.checkpoint))
        out = cp.anchor(checkpoint, Path(args.out_dir or Path(args.checkpoint).parent), calendars=args.calendar or cp.DEFAULT_CALENDARS)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(f"not anchored: {exc}")
    cp.save(out, Path(args.checkpoint))
    added = len(out["anchors"]) - len(checkpoint.get("anchors") or [])
    print(f"root {checkpoint['root'][:16]}... submitted to {added} calendar(s); proofs are pending until Bitcoin confirms (hours)")
    for anchor in out["anchors"][-added:]:
        print(f"  {anchor['file']}")
    return 0


# -- trace ------------------------------------------------------------------------------


def cmd_trace(args: argparse.Namespace) -> int:
    """Walk a decision's parent links across exports and report the first step that fails."""
    from warrant.trace import trace

    try:
        records: List[Dict[str, Any]] = []
        for path in args.export:
            records.extend(_read_jsonl(path))
        ring = _keyring(args.keys)
        steps = trace(args.record, records, ring)
    except (FileNotFoundError, ValueError) as exc:
        return _fail(str(exc))
    if args.json:
        json.dump([s.to_dict() for s in steps], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for step in steps:
            print(step.describe())
    first = next((s for s in steps if s.problems), None)
    if first is None:
        print("every step verifies; no fault is visible in the records" if not args.json else "", file=sys.stderr if args.json else sys.stdout)
        return 0
    if not args.json:
        print(f"first failing step: {first.record_id} ({first.issuer}, {first.decision_class})")
    return 1


# -- evidence and erasure ---------------------------------------------------------------


def cmd_evidence_check(args: argparse.Namespace) -> int:
    """Does an artefact (and, for a sensitive item, its salt) produce a recorded digest?"""
    from warrant.hashing import content_hash, salted_hash

    try:
        raw = Path(args.file).read_bytes()
    except FileNotFoundError:
        return _fail(f"{args.file}: file not found")
    content: Any = raw
    if args.json:
        try:
            content = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _fail(f"{args.file}: not JSON ({exc.msg})")
    if args.salt:
        try:
            salt = bytes.fromhex(args.salt)
            digest = salted_hash(content, salt)
        except ValueError as exc:
            return _fail(f"bad salt: {exc}")
    else:
        digest = content_hash(content)
    if digest == args.hash:
        print(f"MATCH: {args.file} produces {digest}")
        return 0
    if args.json:
        # A digest made by a Python SDK before 0.9.0 covers Python's own number formatting.
        from warrant.hashing import legacy_canonical_json, sha256_hex

        legacy = legacy_canonical_json(content).encode("utf-8")
        if sha256_hex((bytes.fromhex(args.salt) if args.salt else b"") + legacy) == args.hash:
            print(f"MATCH: {args.file} produces {args.hash} under the JSON encoding used before 0.9.0")
            return 0
    print(f"NO MATCH: {args.file} produces {digest}, the record says {args.hash}")
    return 1


def cmd_erase(args: argparse.Namespace) -> int:
    """Delete sidecar entries (content and salt). Records are never touched and still verify."""
    from warrant.store import open_store

    try:
        store = open_store(args.store)
    except Exception as exc:  # a bad DSN or an unreachable database
        return _fail(f"cannot open store {args.store!r}: {exc}")
    try:
        if not hasattr(store, "erase_blob"):
            return _fail("this store has no sidecar")
        erased = [h for h in args.hash if store.erase_blob(h)]
    finally:
        store.close()
    missing = [h for h in args.hash if h not in erased]
    for h in erased:
        print(f"erased sidecar {h}")
    for h in missing:
        print(f"nothing held for {h}", file=sys.stderr)
    print("the records that cite these digests are unchanged and still verify")
    return 0


def add_parsers(sub: Any) -> None:
    keys = sub.add_parser("keys", help="issuer signing keys (Ed25519)")
    ks = keys.add_subparsers(dest="keys_command", required=True)
    kg = ks.add_parser("generate", help="generate an issuer key pair")
    kg.add_argument("--issuer", required=True, help="the organisation that signs, e.g. demo-bank")
    kg.add_argument("--out", default=".warrant/keys", metavar="DIR")
    kg.set_defaults(func=cmd_keys_generate)
    ksh = ks.add_parser("show", help="show a private key's public half, or a key set")
    ksh.add_argument("file")
    ksh.add_argument("--public", action="store_true", help="print the public key set as JSON")
    ksh.set_defaults(func=cmd_keys_show)
    kr = ks.add_parser("revoke", help="mark a key revoked in a published key set")
    kr.add_argument("keyset")
    kr.add_argument("key_id")
    kr.add_argument("--at", help="revocation time (default now)")
    kr.set_defaults(func=cmd_keys_revoke)

    cp = sub.add_parser("checkpoint", help="checkpoints: signed Merkle commitments, witnesses and anchoring")
    cs = cp.add_subparsers(dest="checkpoint_command", required=True)
    cc = cs.add_parser("create", help="sign a checkpoint over a whole chain")
    src = cc.add_mutually_exclusive_group(required=True)
    src.add_argument("--store", metavar="URL")
    src.add_argument("--export", metavar="FILE")
    cc.add_argument("--stream", required=True)
    cc.add_argument("--tenant")
    cc.add_argument("--key", required=True, metavar="FILE", help="the issuer's private key")
    cc.add_argument("--previous", metavar="FILE", help="the last checkpoint; embeds a consistency proof from it")
    cc.add_argument("-o", "--output", required=True, metavar="FILE")
    cc.set_defaults(func=cmd_checkpoint_create)
    co = cs.add_parser("cosign", help="co-sign as a witness, refusing anything inconsistent with what it last signed")
    co.add_argument("checkpoint")
    co.add_argument("--key", required=True, metavar="FILE", help="the witness's private key")
    co.add_argument("--keys", action="append", metavar="FILE", help="the issuer's public key set")
    co.add_argument("--state", metavar="DIR", help="where this witness remembers the last checkpoint per chain")
    co.add_argument("-o", "--output", metavar="FILE")
    co.set_defaults(func=cmd_checkpoint_cosign)
    cv = cs.add_parser("verify", help="check a checkpoint's signatures and, with --export, its root")
    cv.add_argument("checkpoint")
    cv.add_argument("--keys", action="append", metavar="FILE")
    cv.add_argument("--export", metavar="FILE")
    cv.set_defaults(func=cmd_checkpoint_verify)
    cpr = cs.add_parser("prove", help="an inclusion proof for one record")
    cpr.add_argument("checkpoint")
    cpr.add_argument("--export", required=True, metavar="FILE")
    cpr.add_argument("--record", required=True, metavar="RECORD_ID")
    cpr.add_argument("-o", "--output", metavar="FILE")
    cpr.set_defaults(func=cmd_checkpoint_prove)
    ca = cs.add_parser("anchor", help="submit the root to OpenTimestamps calendars (optional, level 3)")
    ca.add_argument("checkpoint")
    ca.add_argument("--calendar", action="append", metavar="URL")
    ca.add_argument("--out-dir", metavar="DIR")
    ca.set_defaults(func=cmd_checkpoint_anchor)

    tr = sub.add_parser("trace", help="walk a decision's parent links across organisations and find the first failing step")
    tr.add_argument("record", metavar="RECORD_ID")
    tr.add_argument("--export", action="append", required=True, metavar="FILE", help="an export from each organisation involved")
    tr.add_argument("--keys", action="append", metavar="FILE", help="their public key sets")
    tr.add_argument("--json", action="store_true")
    tr.set_defaults(func=cmd_trace)

    ev = sub.add_parser("evidence", help="check an artefact against a recorded digest")
    es = ev.add_subparsers(dest="evidence_command", required=True)
    ec = es.add_parser("check", help="does this file produce the digest on the record?")
    ec.add_argument("--hash", required=True)
    ec.add_argument("--file", required=True)
    ec.add_argument("--salt", help="hex salt from the issuer's sidecar, for a sensitive item")
    ec.add_argument("--json", action="store_true", help="hash the file's JSON content canonically, as the SDK does for objects")
    ec.set_defaults(func=cmd_evidence_check)

    er = sub.add_parser("erase", help="delete sidecar entries (content and salt); records are never touched")
    er.add_argument("--store", required=True, metavar="URL")
    er.add_argument("--hash", action="append", required=True, metavar="DIGEST")
    er.set_defaults(func=cmd_erase)
