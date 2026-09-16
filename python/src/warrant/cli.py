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


def _workspace(args: argparse.Namespace):
    from pathlib import Path

    return Path(args.workspace)


def _open_store(path: str):
    try:
        return SQLiteStore(path, read_only=True), None
    except FileNotFoundError as exc:
        return None, str(exc)


def _cmd_set_create(args: argparse.Namespace) -> int:
    from warrant.sets import build_set

    store, error = _open_store(args.store)
    if store is None:
        print(error, file=sys.stderr)
        return 1
    try:
        decision_set = build_set(store, args.name, stream=args.stream, where=args.where, limit=args.limit, subjects=args.subject or None)
    except (ImportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
    if not decision_set.items:
        print(f"no decisions matched in stream {args.stream!r}", file=sys.stderr)
        return 1
    path = decision_set.save(_workspace(args) / "sets" / f"{args.name}.jsonl")
    with_content = sum(1 for i in decision_set if i.evidence_content)
    with_inputs = sum(1 for i in decision_set if i.record["decision"].get("inputs") is not None)
    print(f"set {args.name}: {len(decision_set)} decision(s) saved to {path}")
    print(f"  {with_inputs} with recorded inputs, {with_content} with captured evidence content")
    if with_inputs < len(decision_set):
        print("  decisions without inputs cannot be replayed; record with capture_inputs=True", file=sys.stderr)
    return 0


def _cmd_set_list(args: argparse.Namespace) -> int:
    from warrant.sets import DecisionSet

    folder = _workspace(args) / "sets"
    files = sorted(folder.glob("*.jsonl")) if folder.exists() else []
    if not files:
        print("no sets")
        return 0
    for f in files:
        try:
            ds = DecisionSet.load(f)
            print(f"{ds.name}: {len(ds)} decision(s), created {ds.created_at}, from stream {ds.source.get('stream')}")
        except (ValueError, OSError) as exc:
            print(f"{f.name}: unreadable ({exc})", file=sys.stderr)
    return 0


def _cmd_target_add(args: argparse.Namespace) -> int:
    from warrant.replay import Target, load_targets, parse_agent, save_targets

    try:
        agent = parse_agent(args.agent)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = _workspace(args) / "targets.json"
    try:
        targets = load_targets(path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    try:
        params = _parse_kv(args.param or [])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    targets[args.name] = Target(args.name, agent, model=args.model, policy=args.policy, decider=args.decider, params=params)
    save_targets(path, targets)
    extra = f", params {params}" if params else ""
    print(f"target {args.name}: {agent.name}@{agent.version}, model {args.model or '-'}, policy {args.policy or '-'}, decider {args.decider or '-'}{extra}")
    return 0


def _cmd_target_list(args: argparse.Namespace) -> int:
    from warrant.replay import load_targets

    targets = load_targets(_workspace(args) / "targets.json")
    if not targets:
        print("no targets")
        return 0
    for t in targets.values():
        print(f"{t.name}: {t.agent.name}@{t.agent.version}, model {t.model or '-'}, policy {t.policy or '-'}, decider {t.decider or '-'}")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    from warrant.replay import load_decider, load_targets, replay
    from warrant.sets import DecisionSet

    ws = _workspace(args)
    set_path = ws / "sets" / f"{args.set}.jsonl"
    if not set_path.exists():
        print(f"set {args.set!r} not found at {set_path}", file=sys.stderr)
        return 1
    targets = load_targets(ws / "targets.json")
    target = targets.get(args.against)
    if target is None:
        print(f"target {args.against!r} not found; known: {', '.join(targets) or 'none'}", file=sys.stderr)
        return 1
    decider_spec = args.decider or target.decider
    if not decider_spec:
        print("no decider: pass --decider module:function or set one on the target", file=sys.stderr)
        return 2
    try:
        decider = load_decider(decider_spec)
        decision_set = DecisionSet.load(set_path)
        fail_on = [g.strip() for g in args.fail_on.split(",") if g.strip()] if args.fail_on else []
        max_inc = float(args.max_cost_increase.rstrip("%")) if args.max_cost_increase else None
        report = replay(decision_set, target, decider, mode=args.mode, concurrency=args.concurrency, fail_on=fail_on, max_cost_increase=max_inc)
    except (ImportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.summary())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"# json report: {args.json}")
    if args.junit:
        with open(args.junit, "w", encoding="utf-8") as fh:
            fh.write(report.to_junit())
        print(f"# junit report: {args.junit}")
    return 0 if report.passed else 1


def _cmd_import(args: argparse.Namespace) -> int:
    from warrant.importer import Taxonomy, TaxonomyError, import_traces

    try:
        taxonomy = Taxonomy.load(args.taxonomy)
    except FileNotFoundError:
        print(f"{args.taxonomy}: file not found", file=sys.stderr)
        return 1
    except (TaxonomyError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    policy = None
    label = None
    if args.policy:
        bundle, error = _load_bundle(args.policy)
        if bundle is None:
            print(error, file=sys.stderr)
            return 1
        from warrant.policy import CelPolicyEngine

        policy = CelPolicyEngine(bundle)
        label = ", ".join(f"{p.policy_id}@{p.version}" for p in bundle.policies)
    store = None
    if not args.dry_run:
        store = SQLiteStore(args.store)
    try:
        report = import_traces(args.files, taxonomy, store=store, policy=policy, policy_label=label, stream=args.stream)
    except FileNotFoundError as exc:
        print(f"{exc.filename}: file not found", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    print(report.summary())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"json report: {args.report}")
    return 0 if report.records else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warrant", description="Warrant: the decision ledger for AI agents.")
    parser.add_argument("--version", action="version", version=f"warrant {__version__} (schema v{SCHEMA_VERSION})")
    parser.add_argument("--workspace", default=".warrant", metavar="DIR", help="where sets and targets live (default .warrant)")
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

    st = sub.add_parser("set", help="decision sets: real recorded decisions saved for replay")
    st_sub = st.add_subparsers(dest="set_command")
    st.set_defaults(func=lambda _args: (st.print_help(), 2)[1])
    sc = st_sub.add_parser("create", help="select decisions from a store into a named set")
    sc.add_argument("name")
    sc.add_argument("--store", required=True, metavar="PATH")
    sc.add_argument("--from-stream", dest="stream", required=True, metavar="STREAM")
    sc.add_argument("--where", metavar="CEL", help="filter, e.g. \"outcome.label == 'default'\"")
    sc.add_argument("--limit", type=int)
    sc.add_argument("--subject", action="append", metavar="SUBJECT", help="only these subjects (repeatable)")
    sc.set_defaults(func=_cmd_set_create)
    sl = st_sub.add_parser("list", help="list sets in the workspace")
    sl.set_defaults(func=_cmd_set_list)

    tg = sub.add_parser("target", help="targets: named agent version, model and policy combinations to replay against")
    tg_sub = tg.add_subparsers(dest="target_command")
    tg.set_defaults(func=lambda _args: (tg.print_help(), 2)[1])
    ta = tg_sub.add_parser("add", help="add or replace a target")
    ta.add_argument("name")
    ta.add_argument("--agent", required=True, metavar="NAME@VERSION")
    ta.add_argument("--model", metavar="PROVIDER/MODEL")
    ta.add_argument("--policy", metavar="DIR", help="policy bundle directory")
    ta.add_argument("--decider", metavar="MODULE:FUNCTION", help="the function that makes one decision")
    ta.add_argument("--param", action="append", metavar="KEY=VALUE", help="free-form parameter the decider can read from target.params (repeatable)")
    ta.set_defaults(func=_cmd_target_add)
    tl = tg_sub.add_parser("list", help="list targets")
    tl.set_defaults(func=_cmd_target_list)

    te = sub.add_parser("test", help="replay a set against a target and fail on regressions")
    te.add_argument("set")
    te.add_argument("--against", required=True, metavar="TARGET")
    te.add_argument("--mode", choices=["frozen", "live"], default="frozen")
    te.add_argument("--decider", metavar="MODULE:FUNCTION", help="overrides the target's decider")
    te.add_argument("--fail-on", metavar="GATES", help="comma list of flipped,new-deny,new-escalate,unreplayable,errored")
    te.add_argument("--max-cost-increase", metavar="PCT", help="e.g. 10%%")
    te.add_argument("--concurrency", type=int, default=1)
    te.add_argument("--json", metavar="FILE", help="write a JSON report")
    te.add_argument("--junit", metavar="FILE", help="write a JUnit XML report")
    te.set_defaults(func=_cmd_test)

    im = sub.add_parser("import", help="reconstruct decision records from OpenTelemetry trace exports")
    im.add_argument("files", nargs="+", metavar="FILE", help="OTLP JSON / JSONL or Python SDK console-exporter JSON")
    im.add_argument("--taxonomy", required=True, metavar="FILE", help="which spans are decisions and how to read them")
    im.add_argument("--store", default=".warrant/records.db", metavar="PATH")
    im.add_argument("--stream", metavar="NAME", help="overrides the taxonomy's stream")
    im.add_argument("--policy", metavar="DIR", help="policy bundle to check each decision against, retrospectively")
    im.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    im.add_argument("--report", metavar="FILE", help="write a JSON report")
    im.set_defaults(func=_cmd_import)
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
