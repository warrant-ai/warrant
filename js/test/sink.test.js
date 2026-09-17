import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { gunzipSync } from "node:zlib";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HttpSink, PermanentSinkError, SinkError, Warrant, validate } from "../src/index.js";

const TOKEN = "demo-bank-secret-token-0123456789";
const quiet = { info() {}, warn() {}, error() {} };

/** A stand-in collector that speaks the same protocol as `warrant collector`. */
async function collector(respond) {
  const seen = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      let body = Buffer.concat(chunks);
      if (req.headers["content-encoding"] === "gzip") body = gunzipSync(body);
      const request = { path: req.url, auth: req.headers.authorization, encoding: req.headers["content-encoding"], agent: req.headers["user-agent"], payload: JSON.parse(body.toString("utf8")) };
      seen.push(request);
      const { status, json } = respond(request);
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(json));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, seen, close: () => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }) };
}

test("the SDK delivers valid, unsealed records to a collector with auth and gzip", async () => {
  const c = await collector((request) =>
    request.auth === `Bearer ${TOKEN}` ? { status: 202, json: { accepted: request.payload.records.length, duplicates: 0 } } : { status: 401, json: { detail: "missing or invalid bearer token" } },
  );
  const w = new Warrant("lending", { tenant: "demo-bank", store: `${c.url}/`, token: TOKEN, agent: { name: "a", version: "1" }, spillDir: await mkdtemp(join(tmpdir(), "warrant-sink-")), flushIntervalMs: 5, logger: quiet });
  for (let i = 0; i < 30; i++) {
    await w.decide("credit.approve", { subject: `LN-${i}` }, (d) => {
      d.evidence("bureau", { uri: "cibil://x", content: { i }, excerpt: "x".repeat(40) });
      d.act("approve");
    });
  }
  assert.equal(await w.flush(), true);
  await w.close();
  await c.close();
  const records = c.seen.flatMap((r) => r.payload.records);
  assert.equal(records.length, 30);
  records.forEach((record) => { validate(record); assert.equal("seal" in record, false); });
  assert.ok(c.seen.every((r) => r.path === "/v1/records" && r.agent === "warrantai-js"));
  assert.ok(c.seen.some((r) => r.encoding === "gzip"));
  assert.deepEqual(w.stats(), { submitted: 30, delivered: 30, spilled: 0, recovered: 0, failed_batches: 0, dead: 0, pending: 0, queued: 0 });
});

test("4xx is permanent except 408 and 429; 5xx and network failures are transient", async () => {
  let status = 400;
  const c = await collector(() => ({ status, json: { detail: "1 record(s) rejected", problems: [{ index: 0, error: "tenant: must be string" }] } }));
  const sink = new HttpSink(c.url, "wrong-token-abcdefghijklmnop", { compress: false, logger: quiet });
  await assert.rejects(sink.write([{}]), (err) => err instanceof PermanentSinkError && /\(400\): 1 record\(s\) rejected; first: tenant: must be string/.test(err.message));
  for (const transient of [408, 429, 503]) {
    status = transient;
    await assert.rejects(sink.write([{}]), (err) => err instanceof SinkError && !(err instanceof PermanentSinkError));
  }
  await c.close();
  await assert.rejects(sink.write([{}]), (err) => err instanceof SinkError && !(err instanceof PermanentSinkError) && /unreachable/.test(err.message));
});

test("the sink refuses odd urls and warns about clear-text collectors", () => {
  assert.throws(() => new HttpSink("collector.internal", "t"), /http:\/\/ or https:\/\//);
  const warnings = [];
  new HttpSink("http://collector.internal:8787", "t", { logger: { warn: (m) => warnings.push(m) } });
  new HttpSink("http://127.0.0.1:8787", "t", { logger: { warn: (m) => warnings.push(m) } });
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /not https/);
});
