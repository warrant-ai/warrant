import { test } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { writeFile, mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const BIN = join(HERE, "..", "bin", "warrant.js");
const EXAMPLE = join(HERE, "..", "..", "examples", "loan-approval.json");

async function cli(...args) {
  try {
    const { stdout, stderr } = await run(process.execPath, [BIN, ...args]);
    return { code: 0, stdout, stderr };
  } catch (err) {
    return { code: err.code, stdout: err.stdout, stderr: err.stderr };
  }
}

test("--version prints version and schema version", async () => {
  const { code, stdout } = await cli("--version");
  assert.equal(code, 0);
  assert.match(stdout, /^warrant 0\.0\.1 \(schema v0\)/);
});

test("schema prints the schema", async () => {
  const { code, stdout } = await cli("schema");
  assert.equal(code, 0);
  assert.equal(JSON.parse(stdout).title, "Warrant decision record");
});

test("validate accepts a valid file", async () => {
  const { code, stdout } = await cli("validate", EXAMPLE);
  assert.equal(code, 0);
  assert.match(stdout, /valid/);
});

test("validate rejects an invalid file", async () => {
  const record = JSON.parse(await readFile(EXAMPLE, "utf8"));
  record.origin = "guessed";
  const dir = await mkdtemp(join(tmpdir(), "warrant-"));
  const bad = join(dir, "bad.json");
  await writeFile(bad, JSON.stringify(record));
  const { code, stderr } = await cli("validate", bad);
  assert.equal(code, 1);
  assert.match(stderr, /INVALID/);
  assert.match(stderr, /origin/);
});

test("validate reports a missing file", async () => {
  const { code, stderr } = await cli("validate", "/nonexistent/record.json");
  assert.equal(code, 1);
  assert.match(stderr, /file not found/);
});

test("validate reports malformed JSON", async () => {
  const dir = await mkdtemp(join(tmpdir(), "warrant-"));
  const bad = join(dir, "bad.json");
  await writeFile(bad, "{not json");
  const { code, stderr } = await cli("validate", bad);
  assert.equal(code, 1);
  assert.match(stderr, /invalid JSON/);
});

test("no command prints usage and exits 2", async () => {
  const { code, stdout } = await cli();
  assert.equal(code, 2);
  assert.match(stdout, /usage: warrant/);
});
