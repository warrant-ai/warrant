import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";

import { sha256Hex, utf8 } from "../src/sha256.js";

const node = (bytes) => createHash("sha256").update(bytes).digest("hex");

test("SHA-256 matches node:crypto on the FIPS vectors and on every length across the padding boundaries", () => {
  assert.equal(sha256Hex(new Uint8Array()), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Hex(utf8("abc")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  for (let n = 0; n <= 300; n++) {
    const bytes = randomBytes(n);
    assert.equal(sha256Hex(new Uint8Array(bytes)), node(bytes), `length ${n}`);
  }
  const large = randomBytes(1 << 20);
  assert.equal(sha256Hex(new Uint8Array(large)), node(large));
});

test("UTF-8 matches Buffer, including astral characters and lone surrogates", () => {
  const samples = ["", "ascii", "é", "₹ 4,50,000", "日本語", "😀👩‍💻", "\ud800", "a\udc00b", "\ud83d", "\u0000\u007f\u0080߿ࠀ￿"];
  for (let i = 0; i < 2000; i++) {
    samples.push(Array.from({ length: 1 + (i % 12) }, () => String.fromCharCode(Math.floor(Math.random() * 0x10000))).join(""));
  }
  for (const text of samples) assert.deepEqual(Buffer.from(utf8(text)), Buffer.from(text, "utf8"), JSON.stringify(text));
});
