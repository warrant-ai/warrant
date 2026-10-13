/** ULID generation without a dependency (48-bit millisecond time, 80-bit randomness). */

import { randomBytes } from "node:crypto";

import { ULID_RE, deterministicUlid, encodeUlid } from "./deterministic-ulid.js";

export { ULID_RE, deterministicUlid };

/** Return a 26-character Crockford base32 ULID, lexically sortable by creation time. */
export function ulid(nowMs = Date.now()) {
  if (!Number.isInteger(nowMs) || nowMs < 0 || nowMs >= 2 ** 48) {
    throw new RangeError(`timestamp out of ULID range: ${nowMs}`);
  }
  return encodeUlid((BigInt(nowMs) << 80n) | BigInt(`0x${randomBytes(10).toString("hex")}`));
}
