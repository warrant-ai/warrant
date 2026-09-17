/**
 * Canonical JSON and SHA-256 helpers, matching the Python SDK: keys sorted, no
 * whitespace, non-ASCII kept as is.
 *
 * One difference cannot be closed: JavaScript has a single number type, so a value
 * Python would write as `4.0` is written here as `4`. Hash strings or bytes when a
 * content hash has to be reproduced from another language.
 */

import { createHash } from "node:crypto";

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).filter((k) => value[k] !== undefined).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  const text = JSON.stringify(value);
  if (text === undefined) throw new TypeError(`value is not JSON-serialisable: ${typeof value}`);
  return text;
}

export function sha256Hex(data) {
  return createHash("sha256").update(data).digest("hex");
}

/** Hash evidence content. Bytes are hashed as is, strings as UTF-8, anything else as canonical JSON. */
export function contentHash(content) {
  if (content instanceof Uint8Array) return sha256Hex(content);
  if (content instanceof ArrayBuffer) return sha256Hex(new Uint8Array(content));
  if (typeof content === "string") return sha256Hex(Buffer.from(content, "utf8"));
  try {
    return sha256Hex(Buffer.from(canonicalJson(content), "utf8"));
  } catch (err) {
    throw new TypeError(`evidence content must be bytes, a string or JSON-serialisable: ${err.message}`);
  }
}
