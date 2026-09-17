/** ULID generation without a dependency (48-bit millisecond time, 80-bit randomness). */

import { randomBytes } from "node:crypto";

const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/** Return a 26-character Crockford base32 ULID, lexically sortable by creation time. */
export function ulid(nowMs = Date.now()) {
  if (!Number.isInteger(nowMs) || nowMs < 0 || nowMs >= 2 ** 48) {
    throw new RangeError(`timestamp out of ULID range: ${nowMs}`);
  }
  let value = (BigInt(nowMs) << 80n) | BigInt(`0x${randomBytes(10).toString("hex")}`);
  let out = "";
  for (let i = 0; i < 26; i++) {
    out = ALPHABET[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
}
