"""The ``warrant`` command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from warrant import __version__
from warrant.schema import SCHEMA_VERSION, ValidationError, load_schema, validate
from warrant.store import SQLiteStore
from warrant.verify import verify_records


def _cmd_schema(_: argparse.Namespace) -> int:
    json.dump(load_schema(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    failures = 0
    for path in args.files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except FileNotFoundError:
            print(f"{path}: file not found", file=sys.stderr)
            failures += 1
            continue
        except json.JSONDecodeError as exc:
            print(f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}", file=sys.stderr)
            failures += 1
            continue
        try:
            validate(record)
        except ValidationError as exc:
            failures += 1
            print(f"{path}: INVALID", file=sys.stderr)
            for message in exc.errors:
                print(f"  {message}", file=sys.stderr)
            continue
        print(f"{path}: valid (schema v{SCHEMA_VERSION})")
    return 1 if failures else 0


def _read_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno}: {exc.msg}") from exc


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        reports = verify_records(_read_jsonl(args.file))
    except FileNotFoundError:
        print(f"{args.file}: file not found", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"{args.file}: invalid JSONL: {exc}", file=sys.stderr)
        return 1
    if not reports:
        print(f"{args.file}: no records")
        return 1
    failed = 0
    for report in reports:
        if report.ok:
            print(f"{report.stream}: {report.records} record(s), chain OK")
        else:
            failed += 1
            print(f"{report.stream}: {report.records} record(s), FAILED", file=sys.stderr)
            for message in report.errors:
                print(f"  {message}", file=sys.stderr)
    return 1 if failed else 0


def _cmd_export(args: argparse.Namespace) -> int:
    try:
        store = SQLiteStore(args.store, read_only=True)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
        try:
            count = 0
            for record in store.iter_records(args.stream):
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        finally:
            if out is not sys.stdout:
                out.close()
    finally:
        store.close()
    if args.output:
        print(f"exported {count} record(s) to {args.output}")
    elif count == 0:
        print("no records", file=sys.stderr)
    return 0


def _parse_kv(pairs: List[str]) -> dict:
    inputs = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"expected key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            inputs[key] = json.loads(raw)
        except json.JSONDecodeError:
            inputs[key] = raw
    return inputs


def _load_bundle(path: str):
    from warrant.policy import PolicyBundle, PolicyError

    try:
        return PolicyBundle.load(path), None
    except FileNotFoundError as exc:
        return None, str(exc)
    except PolicyError as exc:
        return None, f"policy error: {exc}"
    except ImportError as exc:
        return None, str(exc)


def _cmd_policy_test(args: argparse.Namespace) -> int:
    from warrant.policy import run_policy_tests

    bundle, error = _load_bundle(args.bundle)
    if bundle is None:
        print(error, file=sys.stderr)
        return 1
    results = run_policy_tests(bundle)
    if not results:
        print(f"{args.bundle}: {len(bundle.policies)} policy file(s), no tests defined")
        return 0
    failed = [r for r in results if not r.passed]
    for r in results:
        mark = "ok  " if r.passed else "FAIL"
        line = f"{mark} {r.policy_id}: {r.name}"
        print(line if r.passed else f"{line}: {r.detail}", file=sys.stdout if r.passed else sys.stderr)
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed across {len(bundle.policies)} policy file(s)")
    return 1 if failed else 0


def _cmd_policy_check(args: argparse.Namespace) -> int:
    from warrant.policy import CelPolicyEngine

    bundle, error = _load_bundle(args.bundle)
    if bundle is None:
        print(error, file=sys.stderr)
        return 1
    try:
        inputs = _parse_kv(args.inputs)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    verdict = CelPolicyEngine(bundle).evaluate(args.decision_class, inputs)
    parts = [verdict.result]
    if verdict.policy_id:
        parts.append(f"policy {verdict.policy_id}@{verdict.policy_version}")
    if verdict.clause:
        parts.append(f"clause {verdict.clause}")
    if verdict.reason:
        parts.append(verdict.reason)
    if verdict.flagged:
        parts.append("FLAGGED")
    print("  ".join(parts))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warrant", description="Warrant: the decision ledger for AI agents.")
    parser.add_argument("--version", action="version", version=f"warrant {__version__} (schema v{SCHEMA_VERSION})")
    sub = parser.add_subparsers(dest="command")

    schema = sub.add_parser("schema", help="print the decision record JSON Schema")
    schema.set_defaults(func=_cmd_schema)

    val = sub.add_parser("validate", help="validate one or more decision record JSON files")
    val.add_argument("files", nargs="+", metavar="FILE")
    val.set_defaults(func=_cmd_validate)

    ver = sub.add_parser("verify", help="verify the hash chain of an exported JSONL file, offline")
    ver.add_argument("file", metavar="FILE")
    ver.set_defaults(func=_cmd_verify)

    exp = sub.add_parser("export", help="export sealed records from a local store as JSONL")
    exp.add_argument("--store", required=True, metavar="PATH", help="path to the SQLite store")
    exp.add_argument("--stream", metavar="NAME", help="only this stream")
    exp.add_argument("-o", "--output", metavar="FILE", help="write here instead of stdout")
    exp.set_defaults(func=_cmd_export)

    pol = sub.add_parser("policy", help="work with a CEL policy bundle")
    pol_sub = pol.add_subparsers(dest="policy_command")
    pol.set_defaults(func=lambda _args: (pol.print_help(), 2)[1])
    pt = pol_sub.add_parser("test", help="run the tests embedded in each policy file")
    pt.add_argument("bundle", metavar="BUNDLE", help="policy directory or file")
    pt.set_defaults(func=_cmd_policy_test)
    pc = pol_sub.add_parser("check", help="evaluate one decision class against key=value inputs")
    pc.add_argument("bundle", metavar="BUNDLE")
    pc.add_argument("decision_class", metavar="CLASS")
    pc.add_argument("inputs", nargs="*", metavar="KEY=VALUE", help="values are parsed as JSON, else strings")
    pc.set_defaults(func=_cmd_policy_check)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
