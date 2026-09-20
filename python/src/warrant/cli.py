"""The ``warrant`` command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
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
    store, error = _open_store(args.store)
    if store is None:
        print(error, file=sys.stderr)
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
    from warrant.store import open_store

    try:
        return open_store(path, read_only=True), None
    except FileNotFoundError as exc:
        return None, str(exc)
    except ImportError as exc:
        return None, str(exc)
    except Exception as exc:  # a bad DSN or an unreachable database
        return None, f"cannot open store {path!r}: {exc}"


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


def _cmd_collector(args: argparse.Namespace) -> int:
    try:
        from warrant.collector import parse_tokens, serve
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        tokens = parse_tokens(",".join(args.token or []) or os.environ.get("WARRANT_COLLECTOR_TOKENS"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not tokens and not args.insecure:
        print("no tokens: pass --token tenant:token (or WARRANT_COLLECTOR_TOKENS), or --insecure for local development", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve(args.store, listen=args.listen, tokens=tokens, insecure=args.insecure)
    except Exception as exc:
        print(f"collector failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    try:
        from warrant.mcp_server import serve
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    from warrant import AgentInfo, Warrant

    # stdout belongs to the MCP transport; everything a person reads goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    agent = AgentInfo(args.agent_name, args.agent_version) if args.agent_name and args.agent_version else None
    try:
        client = Warrant(args.stream, tenant=args.tenant, store=args.store, token=args.token, agent=agent,
                         policy_bundle=args.policy, currency=args.currency, capture_inputs=args.capture_inputs)
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"warrant mcp: {exc}", file=sys.stderr)
        return 2
    serve(client)
    return 0


def _open_existing_store(url: str, *, read_only: bool = False):
    """Open a store that must already exist, reporting a missing file rather than creating an empty one."""
    from pathlib import Path

    if not url.startswith(("postgres://", "postgresql://")) and not Path(url).exists():
        print(f"{url}: no store there. Record some decisions first, or pass --store", file=sys.stderr)
        return None
    from warrant.store import open_store

    return open_store(url, read_only=read_only)


def _cmd_outcomes_ingest(args: argparse.Namespace) -> int:
    from warrant.outcomes import OutcomeFileError, ingest_outcomes

    store = _open_existing_store(args.store)
    if store is None:
        return 1
    try:
        report = ingest_outcomes(
            args.files,
            store,
            stream=args.stream,
            source=args.source,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"{exc.filename}: file not found", file=sys.stderr)
        return 1
    except (OutcomeFileError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
    print(report.summary())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"json report: {args.report}")
    if report.invalid:
        return 1
    return 0 if report.matched else 1


def _cmd_outcomes_status(args: argparse.Namespace) -> int:
    from warrant.outcomes import coverage

    store = _open_existing_store(args.store, read_only=True)
    if store is None:
        return 1
    try:
        report = coverage(store, stream=args.stream)
    finally:
        store.close()
    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(report.summary())
    return 0 if report.decisions else 1


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from warrant.calibrate import CalibrationError, calibrate, gate

    store = _open_existing_store(args.store, read_only=True)
    if store is None:
        return 1
    try:
        report = calibrate(
            store,
            correct_when=args.correct_when,
            stream=args.stream,
            answer=args.answer,
            buckets=args.buckets,
            by=args.by,
            where=args.where,
        )
    except (CalibrationError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
    print(report.summary())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"json report: {args.report}")
    failures = gate(report, max_ece=args.max_ece, max_mce=args.max_mce)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    if failures:
        return 1
    return 0 if report.usable else 1


def _cmd_pack(args: argparse.Namespace) -> int:
    from pathlib import Path

    from warrant.calibrate import CalibrationError
    from warrant.pack import PackError, build_pack

    store = _open_existing_store(args.store, read_only=True)
    if store is None:
        return 1
    try:
        result = build_pack(
            store,
            Path(args.output),
            stream=args.stream,
            policy_dir=Path(args.policy) if args.policy else None,
            questions_dir=Path(args.questions) if args.questions else None,
            correct_when=args.correct_when,
            answer=args.answer,
            by=args.by,
            where=args.where,
            title=args.title,
        )
    except (PackError, CalibrationError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{args.output}: {exc.strerror}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(result.summary())
    return 0


def _cmd_questions_lint(args: argparse.Namespace) -> int:
    from warrant.questions import QuestionSetError, Registry, lint

    try:
        registry = Registry.load(args.registry)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except QuestionSetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = lint(registry)
    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(report.summary())
    return 0 if report.ok else 1


def _cmd_questions_diff(args: argparse.Namespace) -> int:
    from warrant.questions import QuestionSetError, Registry, compare

    try:
        registry = Registry.load(args.registry)
        before = _resolve_ref(registry, args.before)
        after = _resolve_ref(registry, args.after, default_id=before.id)
    except (FileNotFoundError, QuestionSetError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    comparison = compare(before, after)
    if args.json:
        json.dump(comparison.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(comparison.summary())
    return 0 if comparison.sufficient else 1


def _resolve_ref(registry, ref: str, default_id: Optional[str] = None):
    """Accept ``set.id@1.2.3`` or a bare ``1.2.3`` once the set is known from the other side."""
    from warrant.questions import QuestionSetError

    if "@" in ref:
        set_id, _, version = ref.partition("@")
        return registry.get(set_id, version)
    if default_id is None:
        raise QuestionSetError(f"{ref!r} needs a set id, e.g. aml.alert@{ref}")
    return registry.get(default_id, ref)


def _cmd_questions_show(args: argparse.Namespace) -> int:
    from warrant.questions import QuestionSetError, Registry

    try:
        registry = Registry.load(args.registry)
        question_set = (
            _resolve_ref(registry, args.ref) if "@" in args.ref else registry.latest(args.ref)
        )
    except (FileNotFoundError, QuestionSetError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(question_set.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


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
    exp.add_argument("--store", required=True, metavar="URL", help="SQLite path or postgresql:// DSN")
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
    sc.add_argument("--store", required=True, metavar="URL", help="SQLite path or postgresql:// DSN")
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

    oc = sub.add_parser("outcomes", help="attach what actually happened to decisions already recorded")
    oc_sub = oc.add_subparsers(dest="outcomes_command")
    oc.set_defaults(func=lambda _args: (oc.print_help(), 2)[1])
    oi = oc_sub.add_parser("ingest", help="read a CSV of realised outcomes and link each row to its decision")
    oi.add_argument("files", nargs="+", metavar="FILE", help="CSV with a label column and subject or decision_record_id")
    oi.add_argument("--store", default=".warrant/records.db", metavar="URL", help="SQLite path or postgresql:// DSN")
    oi.add_argument("--stream", required=True, metavar="NAME", help="scopes subject lookups")
    oi.add_argument("--source", metavar="NAME", help="where the outcomes came from, when the file does not say")
    oi.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    oi.add_argument("--report", metavar="FILE", help="write a JSON report")
    oi.set_defaults(func=_cmd_outcomes_ingest)
    ost = oc_sub.add_parser("status", help="outcome-attached share, overall and per decision class")
    ost.add_argument("--store", default=".warrant/records.db", metavar="URL")
    ost.add_argument("--stream", metavar="NAME")
    ost.add_argument("--json", action="store_true", help="print the report as JSON")
    ost.set_defaults(func=_cmd_outcomes_status)

    cal = sub.add_parser("calibrate", help="did stated confidence match what happened? ECE and a reliability curve")
    cal.add_argument("--store", default=".warrant/records.db", metavar="URL")
    cal.add_argument("--stream", metavar="NAME")
    cal.add_argument("--correct-when", required=True, metavar="CEL",
                     help="which outcomes vindicate a decision, e.g. \"outcome.label == 'performing'\"")
    cal.add_argument("--answer", metavar="NAME", help="which answer carries the confidence; needed when a decision has several")
    cal.add_argument("--buckets", type=int, default=10, metavar="N", help="confidence bands (default 10)")
    cal.add_argument("--by", metavar="DIM", help="break down by class, question_set, route or inputs.<field>")
    cal.add_argument("--where", metavar="CEL", help="only these decisions, e.g. \"decision.route == 'auto'\"")
    cal.add_argument("--report", metavar="FILE", help="write a JSON report")
    cal.add_argument("--max-ece", type=float, metavar="X", help="fail if Expected Calibration Error exceeds this")
    cal.add_argument("--max-mce", type=float, metavar="X", help="fail if the worst band exceeds this")
    cal.set_defaults(func=_cmd_calibrate)

    pk = sub.add_parser("pack", help="assemble an evidence pack: the records, the proof they are intact, and what they add up to")
    pk.add_argument("--store", default=".warrant/records.db", metavar="URL")
    pk.add_argument("--stream", metavar="NAME")
    pk.add_argument("-o", "--output", required=True, metavar="DIR", help="a new or empty directory")
    pk.add_argument("--policy", metavar="DIR", help="include the policy text that applied in this period")
    pk.add_argument("--questions", metavar="DIR", help="include the question sets the decisions were made by answering")
    pk.add_argument("--correct-when", metavar="CEL", help="add a calibration section; which outcomes vindicate a decision")
    pk.add_argument("--answer", metavar="NAME", help="which answer carries the confidence")
    pk.add_argument("--by", metavar="DIM", help="break the curve down by class, question_set, route or inputs.<field>")
    pk.add_argument("--where", metavar="CEL", help="restrict the curve to these decisions, e.g. \"decision.route == 'auto'\"")
    pk.add_argument("--title", metavar="TEXT", help="heading for the front page")
    pk.set_defaults(func=_cmd_pack)

    qs = sub.add_parser("questions", help="question sets: the questions a decision was made by answering, versioned")
    qs_sub = qs.add_subparsers(dest="questions_command")
    qs.set_defaults(func=lambda _args: (qs.print_help(), 2)[1])
    ql = qs_sub.add_parser("lint", help="check every version bump is big enough for the change it carries")
    ql.add_argument("registry", metavar="DIR", help="directory of question set files")
    ql.add_argument("--json", action="store_true", help="print the report as JSON")
    ql.set_defaults(func=_cmd_questions_lint)
    qd = qs_sub.add_parser("diff", help="what changed between two versions, and whether the bump was sufficient")
    qd.add_argument("registry", metavar="DIR")
    qd.add_argument("before", metavar="ID@VERSION")
    qd.add_argument("after", metavar="VERSION", help="or ID@VERSION")
    qd.add_argument("--json", action="store_true")
    qd.set_defaults(func=_cmd_questions_diff)
    qsh = qs_sub.add_parser("show", help="print one version as JSON, the form it takes in an evidence pack")
    qsh.add_argument("registry", metavar="DIR")
    qsh.add_argument("ref", metavar="ID[@VERSION]", help="defaults to the latest version of that set")
    qsh.set_defaults(func=_cmd_questions_show)

    im = sub.add_parser("import", help="reconstruct decision records from OpenTelemetry trace exports")
    im.add_argument("files", nargs="+", metavar="FILE", help="OTLP JSON / JSONL or Python SDK console-exporter JSON")
    im.add_argument("--taxonomy", required=True, metavar="FILE", help="which spans are decisions and how to read them")
    im.add_argument("--store", default=".warrant/records.db", metavar="PATH")
    im.add_argument("--stream", metavar="NAME", help="overrides the taxonomy's stream")
    im.add_argument("--policy", metavar="DIR", help="policy bundle to check each decision against, retrospectively")
    im.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    im.add_argument("--report", metavar="FILE", help="write a JSON report")
    im.set_defaults(func=_cmd_import)

    co = sub.add_parser("collector", help="run the collector: receives record batches over HTTP and seals them into a store")
    co.add_argument("--store", default=os.environ.get("WARRANT_STORE", ".warrant/records.db"), metavar="URL", help="postgresql://... or a SQLite path")
    co.add_argument("--listen", default="127.0.0.1:8787", metavar="HOST:PORT")
    co.add_argument("--token", action="append", metavar="TENANT:TOKEN", help="bearer token per tenant (repeatable); or WARRANT_COLLECTOR_TOKENS")
    co.add_argument("--insecure", action="store_true", help="no authentication; local development only")
    co.set_defaults(func=_cmd_collector)

    mc = sub.add_parser("mcp", help="run an MCP server over stdio so an MCP-capable agent can query its mandate and record decisions")
    mc.add_argument("--stream", required=True, metavar="NAME")
    mc.add_argument("--store", default=None, metavar="URL", help="SQLite path, postgresql:// DSN or collector URL (default $WARRANT_STORE or .warrant/records.db)")
    mc.add_argument("--token", default=None, help="collector bearer token (default $WARRANT_TOKEN)")
    mc.add_argument("--tenant", default=None)
    mc.add_argument("--policy", default=None, metavar="DIR", help="policy bundle to check decisions against")
    mc.add_argument("--agent-name", default=None, help="who the records say acted (default $WARRANT_AGENT_NAME); the agent cannot set this itself")
    mc.add_argument("--agent-version", default=None)
    mc.add_argument("--currency", default=None)
    mc.add_argument("--capture-inputs", action="store_true", help="store check inputs on the record; for development and staging")
    mc.set_defaults(func=_cmd_mcp)
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
