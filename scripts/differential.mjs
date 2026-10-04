// The Node half of scripts/differential.py: reproduce what Python canonicalised, hashed, sealed
// and signed, then seal and sign the same bodies here for Python to verify.
//
//   node scripts/differential.mjs <cases-from-python.jsonl> <sealed-by-node.jsonl>
import { createReadStream, createWriteStream } from "node:fs";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { CANON, canonicalJson, contentHash, recordHash } from "../js/src/hashing.js";
import { Keyring, PublicKey, SigningKey, verifySeal } from "../js/src/signing.js";

const [casesPath, sealedPath] = process.argv.slice(2);
if (!casesPath || !sealedPath) {
  console.error("usage: node scripts/differential.mjs <cases.jsonl> <sealed.jsonl>");
  process.exit(2);
}

const key = SigningKey.fromPrivateBytes("differential-node", Buffer.alloc(32, 9));
const out = createWriteStream(sealedPath, { encoding: "utf8" });
out.write(`${JSON.stringify({ key: key.public.toEntry() })}\n`);

const failures = [];
const fail = (id, what, got, want) => failures.push(`${id}: ${what}\n  javascript ${got}\n  python     ${want}`);
let ring = null;
let prev = null;
let sequence = 0;

for await (const line of createInterface({ input: createReadStream(casesPath, { encoding: "utf8" }), crlfDelay: Infinity })) {
  if (!line) continue;
  const item = JSON.parse(line);
  if (ring === null) {
    ring = new Keyring([PublicKey.fromEntry(item.key)]);
    continue;
  }
  const { record } = item;
  const id = record.record_id;
  const canonical = canonicalJson(item.value);
  if (canonical !== item.canonical) fail(id, "canonical JSON differs", canonical, item.canonical);
  const digest = contentHash(item.value);
  if (digest !== item.content_hash) fail(id, "content hash differs", digest, item.content_hash);
  const hash = recordHash(record, item.prev);
  if (hash !== record.seal.hash) fail(id, "record hash differs", hash, record.seal.hash);
  const signature = verifySeal(record, ring);
  if (!signature.ok) fail(id, "the Python signature is rejected", signature.reason, "a valid signature");

  sequence += 1;
  const body = { record_id: id, timestamp: record.timestamp, payload: item.value, sequence, seal: { canon: CANON } };
  const sealedHash = recordHash(body, prev);
  body.seal = { canon: CANON, prev_hash: prev, hash: sealedHash, ...key.signSeal(sealedHash, "2026-10-04T00:00:01.000Z") };
  if (!out.write(`${JSON.stringify({ prev, record: body })}\n`)) await once(out, "drain");
  prev = sealedHash;
}

out.end();
await once(out, "finish");
if (failures.length) {
  console.error(`${failures.length} disagreement(s); the first ${Math.min(failures.length, 20)}:`);
  console.error(failures.slice(0, 20).join("\n"));
  process.exit(1);
}
