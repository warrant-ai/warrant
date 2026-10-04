#!/usr/bin/env python3
"""Differential test of the two SDKs: seal in one language, verify in the other.

Generates random JSON values and records from a seed. Python writes each value's canonical JSON
and content hash and seals and signs each record; Node (``scripts/differential.mjs``) must
reproduce all of it, then seals and signs the same bodies itself for Python to verify. The
conformance vectors pin the cases we thought of; this looks for the ones we did not.

    python scripts/differential.py --count 20000            # a fresh seed, printed on failure
    python scripts/differential.py --seed 7 --count 500     # reproduce a run

Values stay inside what the two languages can both represent: integers within ±2^53 and no lone
surrogates (documented as non-portable in the specification, 5.1).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from warrant.hashing import canonical_json, content_hash, record_hash
from warrant.signing import Keyring, PublicKey, SigningKey, verify_seal
from warrant.store import seal_record

NODE_SCRIPT = Path(__file__).with_name("differential.mjs")
MAX_SAFE = 2**53 - 1
# Quotes, escapes, control characters, the JS line separators, scripts a record will carry, and
# keys whose order differs between UTF-16 code units and code points (U+FB33 against an astral).
ALPHABET = list("abzAZ019 _-\"\\/\b\f\n\r\t\x00\x1f\x7f  é€ह்ಕ中דּ￿") + ["\U0001f600", "\U00010000", "\U0010ffff"]
NUMBERS: List[Any] = [0, -0.0, 1, -1, 1.0, 4.0, 0.1, 1e-7, 1e-6, 1e20, 1e21, 1e16, 123456789012345680000.0,
                      5e-324, 1.7976931348623157e308, 0.000001, 333333333.33333329, MAX_SAFE, -MAX_SAFE]


class DifferentialFailure(Exception):
    """The two SDKs disagreed, or the Node side could not run."""


def _number(rng: random.Random) -> Any:
    pick = rng.random()
    if pick < 0.2:
        return rng.choice(NUMBERS)
    if pick < 0.4:
        return rng.randint(-MAX_SAFE, MAX_SAFE)
    if pick < 0.6:
        return round(rng.uniform(-1e6, 1e6), rng.randint(0, 6))  # money and confidences
    while True:  # any finite double, bit pattern by bit pattern
        value = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if math.isfinite(value):
            return value


def _text(rng: random.Random) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))


def _value(rng: random.Random, depth: int = 0) -> Any:
    pick = rng.random()
    if depth < 4 and pick < 0.2:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    if depth < 4 and pick < 0.45:
        return {_text(rng): _value(rng, depth + 1) for _ in range(rng.randint(0, 5))}
    if pick < 0.7:
        return _number(rng)
    if pick < 0.9:
        return _text(rng)
    return rng.choice([None, True, False])


def _cases(seed: int, count: int, key: SigningKey) -> Iterator[Dict[str, Any]]:
    rng = random.Random(seed)
    prev: Optional[str] = None
    for index in range(count):
        value = _value(rng)
        body = {"record_id": f"case-{index}", "timestamp": "2026-10-04T00:00:00.000Z", "payload": value}
        record = seal_record(body, index + 1, prev, key)
        yield {"value": value, "canonical": canonical_json(value), "content_hash": content_hash(value), "prev": prev, "record": record}
        prev = record["seal"]["hash"]


def run(seed: int, count: int, node: str = "node") -> int:
    """Run both directions. Returns the number of cases; raises ``DifferentialFailure`` on any disagreement."""
    if count < 1:
        raise ValueError("count must be at least 1")
    key = SigningKey("differential-python", bytes(range(32)))
    with tempfile.TemporaryDirectory() as directory:
        cases, sealed = Path(directory, "python.jsonl"), Path(directory, "node.jsonl")
        with cases.open("w", encoding="utf-8") as out:
            out.write(json.dumps({"key": key.public.to_dict()}) + "\n")
            for case in _cases(seed, count, key):
                out.write(json.dumps(case) + "\n")
        try:
            done = subprocess.run([node, str(NODE_SCRIPT), str(cases), str(sealed)], capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DifferentialFailure(f"could not run {node}: {exc}") from exc
        if done.returncode != 0:
            raise DifferentialFailure(f"seed {seed}: JavaScript disagrees with Python\n{done.stderr.strip()}")
        failures: List[str] = []
        with sealed.open(encoding="utf-8") as lines:
            ring = Keyring([PublicKey.from_dict(json.loads(next(lines))["key"])])
            checked = 0
            for line in lines:
                case = json.loads(line)
                record = case["record"]
                checked += 1
                if record_hash(record, case["prev"]) != record["seal"]["hash"]:
                    failures.append(f"{record['record_id']}: Python does not reproduce the hash JavaScript sealed")
                ok, why = verify_seal(record, ring)
                if not ok:
                    failures.append(f"{record['record_id']}: Python rejects the JavaScript signature: {why}")
        if checked != count:
            failures.append(f"JavaScript sealed {checked} records, expected {count}")
        if failures:
            raise DifferentialFailure(f"seed {seed}: Python disagrees with JavaScript\n" + "\n".join(failures[:20]))
    return count


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=None, help="default: a fresh one")
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--node", default="node")
    args = parser.parse_args(argv)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    try:
        count = run(seed, args.count, args.node)
    except DifferentialFailure as exc:
        print(exc, file=sys.stderr)
        print(f"reproduce with: python scripts/differential.py --seed {seed} --count {args.count}", file=sys.stderr)
        return 1
    print(f"seed {seed}: {count} values and {count} records agree in both directions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
