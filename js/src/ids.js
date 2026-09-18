/** ULID generation without a dependency (48-bit millisecond time, 80-bit randomness). */

import { createHash, randomBytes } from "node:crypto";

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/** Return a 26-character Crockford base32 ULID, lexically sortable by creation time. */
export function ulid(nowMs = Date.now()) {
  if (!Number.isInteger(nowMs) || nowMs < 0 || nowMs >= 2 ** 48) {
    throw new RangeError(`timestamp out of ULID range: ${nowMs}`);
  }
  return encode((BigInt(nowMs) << 80n) | BigInt(`0x${randomBytes(10).toString("hex")}`));
}

/**
 * ULID whose random part is derived from `key`, so the same input always yields the same id.
 * For events that may be recorded more than once (a retried delivery), where the store's
 * duplicate check by record id must recognise the repeat. Same bytes as the Python SDK.
 */
export function deterministicUlid(tsMs, key) {
  if (typeof key !== "string") throw new TypeError("key must be a string");
  const ts = BigInt(Math.max(0, Math.min(Math.trunc(tsMs), 2 ** 48 - 1)));
  const rand = BigInt(`0x${createHash("sha256").update(key, "utf8").digest("hex").slice(0, 20)}`);
  return encode((ts << 80n) | rand);
}

function encode(value) {
  let out = "";
  for (let i = 0; i < 26; i++) {
    out = ALPHABET[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
}
