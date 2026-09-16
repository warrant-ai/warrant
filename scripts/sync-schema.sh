#!/usr/bin/env sh
# Copies the canonical schema into both packages. Run after editing schema/.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cp "$ROOT/schema/decision-record.v0.json" "$ROOT/python/src/warrant/schema/decision-record.v0.json"
cp "$ROOT/schema/decision-record.v0.json" "$ROOT/js/schema/decision-record.v0.json"
echo "schema synced"
