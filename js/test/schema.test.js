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

test("version and schema version are set", async () => {
  assert.equal(VERSION, JSON.parse(await readFile(join(ROOT, "js", "package.json"), "utf8")).version);
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

// The question-set, state and answer fields are additive and optional inside schema v0.
async function answered(overrides = {}) {
  const record = await example("loan-approval.json");
  Object.assign(record.decision, {
    route: "auto",
    question_set: { id: "credit.approve", version: "3.1.0" },
    state_digest: "a".repeat(64),
    state_ref: "warrant://snapshot/01J8Z5M0000000000000000000",
    answers: [
      {
        question: "disposition",
        value: "approve",
        confidence: 0.94,
        distribution: [
          { value: "approve", p: 0.94 },
          { value: "refer", p: 0.05 },
          { value: "decline", p: 0.01 },
        ],
      },
      { question: "affordability", value: 0.72, confidence: 0.81 },
      { question: "explanation_on_file", value: true },
    ],
  }, overrides);
  return record;
}

test("decision with a question set and answers is valid", async () => {
  validate(await answered());
});

test("records without the new fields are still valid", async () => {
  const record = await example("loan-approval.json");
  for (const field of ["route", "question_set", "state_digest", "answers"]) {
    assert.ok(!(field in record.decision));
  }
  validate(record);
});

test("unknown route is rejected", async () => {
  const record = await answered({ route: "autoclose" });
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.startsWith("decision/route")));
});

test("non-semver question set version is rejected", async () => {
  const record = await answered();
  record.decision.question_set.version = "v3";
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("question_set/version")));
});

test("confidence outside zero to one is rejected", async () => {
  const record = await answered();
  record.decision.answers[0].confidence = 1.4;
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("answers/0/confidence")));
});

test("answer without a value is rejected", async () => {
  const record = await answered();
  delete record.decision.answers[0].value;
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("answers/0")));
});

test("unknown field inside an answer is rejected", async () => {
  const record = await answered();
  record.decision.answers[0].rationale = "free text that belongs in an excerpt";
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("rationale")));
});

test("state digest must be a sha256", async () => {
  const record = await answered({ state_digest: "not-a-hash" });
  assert.throws(() => validate(record), (err) => err.errors.some((m) => m.includes("state_digest")));
});
