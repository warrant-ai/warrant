/**
 * Canonical JSON and SHA-256 helpers. The canonical form is RFC 8785 (the JSON
 * Canonicalization Scheme): keys sorted by UTF-16 code unit, no whitespace, non-ASCII
 * kept as is, numbers as ECMAScript writes them. The Python SDK produces the same bytes.
 *
 * Records sealed by a Python store before 0.9.0 carry no `seal.canon` and were hashed over
 * Python's own number formatting (`4.0`, `1e-07`). Where such a record holds a number the
 * two languages write differently, only the Python verifier can reproduce its hash.
 */

import { createHash } from "node:crypto";

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).filter((k) => value[k] !== undefined).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  if (typeof value === "number" && !Number.isFinite(value)) throw new TypeError("NaN and Infinity have no canonical JSON form");
  const text = JSON.stringify(value);
  if (text === undefined) throw new TypeError(`value is not JSON-serialisable: ${typeof value}`);
  return text;
}

/** The canonical form new records are sealed under, written to `seal.canon`. */
export const CANON = "jcs";

export function sha256Hex(data) {
  return createHash("sha256").update(data).digest("hex");
}

/** The bytes a content hash covers: bytes as is, strings as UTF-8, anything else as canonical JSON. */
export function contentBytes(content) {
  if (content instanceof Uint8Array) return Buffer.from(content);
  if (content instanceof ArrayBuffer) return Buffer.from(new Uint8Array(content));
  if (typeof content === "string") return Buffer.from(content, "utf8");
  try {
    return Buffer.from(canonicalJson(content), "utf8");
  } catch (err) {
    throw new TypeError(`evidence content must be bytes, a string or JSON-serialisable: ${err.message}`);
  }
}

/** Hash evidence content. Bytes are hashed as is, strings as UTF-8, anything else as canonical JSON. */
export function contentHash(content) {
  return sha256Hex(contentBytes(content));
}

export const SALT_BYTES = 32;

/** ADR 4: SHA-256(salt || content). A low-variety value cannot be recovered by trying every one. */
export function saltedHash(content, salt) {
  if (!(salt instanceof Uint8Array) || salt.length !== SALT_BYTES) throw new RangeError(`salt must be ${SALT_BYTES} bytes`);
  return sha256Hex(Buffer.concat([Buffer.from(salt), contentBytes(content)]));
}

/**
 * Hash of a record body (every field but `seal`) chained to `prevHash`, as the store seals it.
 * A `seal.canon` other than `jcs` names a form this version does not know, and throws.
 */
export function recordHash(record, prevHash) {
  const canon = record.seal?.canon;
  if (canon !== undefined && canon !== CANON) throw new RangeError(`record is sealed under canonical form ${JSON.stringify(canon)}, which this version does not know`);
  const body = {};
  for (const key of Object.keys(record)) if (key !== "seal") body[key] = record[key];
  return sha256Hex(Buffer.from(`${canonicalJson(body)}\n${prevHash ?? ""}`, "utf8"));
}
