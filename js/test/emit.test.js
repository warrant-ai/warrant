import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Emitter, PermanentSinkError, SinkError } from "../src/index.js";

const quiet = { info() {}, warn() {}, error() {} };
const dir = () => mkdtemp(join(tmpdir(), "warrant-emit-"));
const rec = (i) => ({ record_id: `R${i}` });
const jsonl = async (path) => (await readFile(path, "utf8")).trim().split("\n").map((line) => JSON.parse(line));

test("delivers in batches and reports stats", async () => {
  const batches = [];
  const emitter = new Emitter({ write: async (b) => batches.push(b.length) }, await dir(), { batchSize: 2, flushIntervalMs: 5, logger: quiet });
  for (let i = 0; i < 5; i++) emitter.submit(rec(i));
  assert.equal(await emitter.flush(), true);
  assert.deepEqual(batches, [2, 2, 1]);
  assert.deepEqual(emitter.stats(), { submitted: 5, delivered: 5, spilled: 0, recovered: 0, failed_batches: 0, dead: 0, pending: 0, queued: 0 });
  await emitter.close();
  assert.throws(() => emitter.submit(rec(9)), /closed/);
});

test("a failing sink spills to disk and the records are recovered when it returns", async () => {
  const spill = await dir();
  let up = false;
  const delivered = [];
  const sink = { write: async (b) => { if (!up) throw new SinkError("collector unreachable"); delivered.push(...b.map((r) => r.record_id)); } };
  const emitter = new Emitter(sink, spill, { flushIntervalMs: 5, backoffInitialMs: 10, backoffMaxMs: 20, logger: quiet });
  emitter.submit(rec(1));
  emitter.submit(rec(2));
  assert.equal(await emitter.flush(), true, "spilled records count as safely handed off");
  const files = (await readdir(spill)).filter((n) => n.endsWith(".jsonl"));
  assert.equal(files.length, 1);
  assert.deepEqual(await jsonl(join(spill, files[0])), [rec(1), rec(2)]);
  assert.equal(emitter.stats().spilled, 2);

  up = true;
  const deadline = Date.now() + 2000;
  while (delivered.length < 2 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(delivered, ["R1", "R2"]);
  assert.deepEqual((await readdir(spill)).filter((n) => n.endsWith(".jsonl")), []);
  assert.equal(emitter.stats().recovered, 2);
  await emitter.close();
});

test("a new emitter recovers spill files left by an earlier process", async () => {
  const spill = await dir();
  await writeFile(join(spill, "00000000000000000001-000000.jsonl"), JSON.stringify(rec(7)) + "\n");
  const delivered = [];
  const emitter = new Emitter({ write: async (b) => delivered.push(...b) }, spill, { flushIntervalMs: 5, logger: quiet });
  const deadline = Date.now() + 2000;
  while (!delivered.length && Date.now() < deadline) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(delivered, [rec(7)]);
  await emitter.close();
});

test("a permanently rejected batch is dead-lettered, not retried", async () => {
  const spill = await dir();
  let calls = 0;
  const emitter = new Emitter({ write: async () => { calls += 1; throw new PermanentSinkError("collector rejected batch (400)"); } }, spill, { flushIntervalMs: 5, logger: quiet });
  emitter.submit(rec(1));
  await emitter.flush();
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(calls, 1);
  const dead = await readdir(join(spill, "dead"));
  assert.equal(dead.length, 1);
  assert.deepEqual(await jsonl(join(spill, "dead", dead[0])), [rec(1)]);
  assert.equal(emitter.stats().dead, 1);
  await emitter.close();
});

test("an unreadable spill file is moved aside instead of blocking the queue", async () => {
  const spill = await dir();
  await mkdir(join(spill, "dead"), { recursive: true });
  await writeFile(join(spill, "00000000000000000001-000000.jsonl"), "{not json\n");
  const emitter = new Emitter({ write: async () => {} }, spill, { flushIntervalMs: 5, logger: quiet });
  const deadline = Date.now() + 2000;
  while (!(await readdir(join(spill, "dead"))).length && Date.now() < deadline) await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(await readdir(join(spill, "dead")), ["00000000000000000001-000000.jsonl"]);
  await emitter.close();
});

test("a full buffer spills from the caller, and a hung sink times the flush out", async () => {
  const spill = await dir();
  let release;
  const hung = new Promise((resolve) => (release = resolve));
  const emitter = new Emitter({ write: () => hung }, spill, { maxQueue: 2, batchSize: 10, flushIntervalMs: 1000, logger: quiet });
  emitter.submit(rec(1));
  emitter.submit(rec(2));
  emitter.submit(rec(3));
  assert.equal(emitter.stats().spilled, 1);
  assert.equal(await emitter.flush(30), false);
  release();
  assert.equal(await emitter.flush(1000), true);
  await emitter.close();
  assert.throws(() => new Emitter({}, spill), /write\(records\)/);
  assert.throws(() => new Emitter({ write() {} }, spill, { batchSize: 0 }), RangeError);
});
