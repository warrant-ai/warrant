// Deterministic ULIDs with no Node imports, so Temporal workflow code can bundle them. `ids.js` re-exports them.

import { sha256Hex, utf8 } from "./sha256.js";

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/**
 * ULID whose random part is derived from `key`, so the same input always yields the same id.
 * For events that may be recorded more than once (a retried delivery), where the store's
 * duplicate check by record id must recognise the repeat. Same bytes as the Python SDK.
 */
export function deterministicUlid(tsMs, key) {
  if (typeof key !== "string") throw new TypeError("key must be a string");
  const ts = BigInt(Math.max(0, Math.min(Math.trunc(tsMs), 2 ** 48 - 1)));
  const rand = BigInt(`0x${sha256Hex(utf8(key)).slice(0, 20)}`);
  return encodeUlid((ts << 80n) | rand);
}

/** The 26-character Crockford base32 form of a 128-bit ULID value. */
export function encodeUlid(value) {
  let out = "";
  for (let i = 0; i < 26; i++) {
    out = ALPHABET[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
}
