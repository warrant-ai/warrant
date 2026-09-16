"""The ``warrant`` command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from warrant import __version__
from warrant.schema import SCHEMA_VERSION, ValidationError, load_schema, validate


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warrant", description="Warrant: the decision ledger for AI agents.")
    parser.add_argument("--version", action="version", version=f"warrant {__version__} (schema v{SCHEMA_VERSION})")
    sub = parser.add_subparsers(dest="command")

    schema = sub.add_parser("schema", help="print the decision record JSON Schema")
    schema.set_defaults(func=_cmd_schema)

    val = sub.add_parser("validate", help="validate one or more decision record JSON files")
    val.add_argument("files", nargs="+", metavar="FILE")
    val.set_defaults(func=_cmd_validate)
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
