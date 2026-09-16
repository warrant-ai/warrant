import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { loadSchema, SCHEMA_VERSION, validate, ValidationError, VERSION } from "../src/index.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

async function example(name) {
  return JSON.parse(await readFile(join(ROOT, "examples", name), "utf8"));
}

test("version and schema version are set", () => {
  assert.equal(VERSION, "0.0.1");
  assert.equal(SCHEMA_VERSION, "0");
});

test("packaged schema matches the canonical copy", async (t) => {
  const canonical = join(ROOT, "schema", "decision-record.v0.json");
  if (!existsSync(canonical)) {
    t.skip("canonical schema only present in the source checkout");
    return;
  }
  assert.deepEqual(loadSchema(), JSON.parse(await readFile(canonical, "utf8")));
});

test("loan approval example is valid", async () => {
  validate(await example("loan-approval.json"));
});

test("loan outcome example is valid", async () => {
  validate(await example("loan-outcome.json"));
});

test("decision without mandate is rejected", async () => {
  const record = await example("loan-approval.json");
  delete record.mandate;
  assert.throws(() => validate(record), (err) => err instanceof ValidationError && err.errors.some((m) => m.includes("mandate")));
});

test("unknown mandate result is rejected", async () => {
  const record = await example("loan-approval.json");
  record.mandate.result = "maybe";
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.startsWith("mandate/result")));
});

test("outcome without reference is rejected", async () => {
  const record = await example("loan-outcome.json");
  delete record.references;
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("references")));
});

test("unknown top-level field is rejected", async () => {
  const record = await example("loan-approval.json");
  record.notes = "free text";
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("notes")));
});

test("non-object input is rejected", () => {
  assert.throws(() => validate(["not", "a", "record"]), ValidationError);
  assert.throws(() => validate(null), ValidationError);
});
