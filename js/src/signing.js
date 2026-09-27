/**
 * Issuer signing and verification (ADR 5): Ed25519 over "adr/0.2 seal\n" + seal.hash.
 * Matches the Python reference (warrant.signing): same key ids, messages and reason strings.
 *
 * A valid signature binds a record to the holder of the issuer's key and shows it has not changed
 * since. It does not stop the issuer rewriting its own history; witnessed checkpoints cover that.
 */

import { createHash, createPrivateKey, createPublicKey, sign as edSign, verify as edVerify } from "node:crypto";

export const SEAL_CONTEXT = "adr/0.2 seal\n";
export const CHECKPOINT_CONTEXT = "adr/0.2 checkpoint\n";
const PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
const SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

export class SigningError extends Error {
  constructor(message) {
    super(message);
    this.name = "SigningError";
  }
}

/** `ed25519:` and the first 16 hex characters of SHA-256 over the raw 32-byte public key. */
export function keyIdFor(rawPublic) {
  return `ed25519:${createHash("sha256").update(Buffer.from(rawPublic)).digest("hex").slice(0, 16)}`;
}

function b64(value, what, length) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new SigningError(`${what} must be a base64 string`);
  const raw = Buffer.from(value, "base64");
  if (raw.length !== length) throw new SigningError(`${what} must decode to ${length} bytes, got ${raw.length}`);
  return raw;
}

function parseTs(value, what) {
  const t = typeof value === "string" ? Date.parse(value) : NaN;
  if (Number.isNaN(t)) throw new SigningError(`${what} is not ISO 8601: ${JSON.stringify(value)}`);
  return t;
}

/** One entry of an issuer's published key set. */
export class PublicKey {
  constructor(issuer, raw, { notBefore = null, revokedAt = null } = {}) {
    this.issuer = issuer;
    this.raw = Buffer.from(raw);
    this.keyId = keyIdFor(this.raw);
    this.notBefore = notBefore;
    this.revokedAt = revokedAt;
    this._key = createPublicKey({ key: Buffer.concat([SPKI_PREFIX, this.raw]), format: "der", type: "spki" });
  }

  static fromEntry(entry) {
    if (!entry || typeof entry !== "object") throw new SigningError("a key entry must be an object");
    if ((entry.alg ?? "Ed25519") !== "Ed25519") throw new SigningError(`unsupported key algorithm ${JSON.stringify(entry.alg)}; ADR 0.2 uses Ed25519`);
    if (typeof entry.issuer !== "string" || !entry.issuer) throw new SigningError("a key entry needs an issuer");
    const raw = b64(entry.public_key, "public_key", 32);
    const key = new PublicKey(entry.issuer, raw, { notBefore: entry.not_before ?? null, revokedAt: entry.revoked_at ?? null });
    if (entry.key_id != null && entry.key_id !== key.keyId) throw new SigningError(`key_id ${JSON.stringify(entry.key_id)} does not match its public key (${key.keyId})`);
    if (key.notBefore) parseTs(key.notBefore, "not_before");
    if (key.revokedAt) parseTs(key.revokedAt, "revoked_at");
    return key;
  }

  toEntry() {
    return { issuer: this.issuer, key_id: this.keyId, alg: "Ed25519", public_key: this.raw.toString("base64"), not_before: this.notBefore, revoked_at: this.revokedAt };
  }

  /** Was this key allowed to sign a record stamped `timestamp`? Returns [ok, reason]. */
  validAt(timestamp) {
    const at = parseTs(timestamp, "record timestamp");
    if (this.notBefore && at < parseTs(this.notBefore, "not_before")) return [false, `key ${this.keyId} was not yet valid at ${timestamp}`];
    if (this.revokedAt && at >= parseTs(this.revokedAt, "revoked_at")) return [false, `key ${this.keyId} was revoked at ${this.revokedAt}`];
    return [true, ""];
  }

  verify(message, signatureB64) {
    let signature;
    try {
      signature = b64(signatureB64, "signature", 64);
    } catch {
      return false;
    }
    return edVerify(null, Buffer.from(message, "utf8"), this._key, signature);
  }
}

/** The public keys a verifier trusts, from one or more issuers' published key sets. */
export class Keyring {
  constructor(keys = []) {
    this._keys = new Map();
    for (const key of keys) this.add(key);
  }

  add(key) {
    const existing = this._keys.get(key.keyId);
    if (existing && existing.issuer !== key.issuer) throw new SigningError(`key ${key.keyId} is claimed by two issuers: ${existing.issuer} and ${key.issuer}`);
    // A revocation seen anywhere wins: a later key set that forgot it must not un-revoke.
    if (existing?.revokedAt && !key.revokedAt) return;
    this._keys.set(key.keyId, key);
  }

  get(keyId) {
    return this._keys.get(keyId);
  }

  get size() {
    return this._keys.size;
  }

  [Symbol.iterator]() {
    return this._keys.values();
  }

  /** From parsed key-set documents: `{ keys: [...] }`. Refuses a set holding a private key. */
  static fromKeySets(...sets) {
    const ring = new Keyring();
    for (const set of sets) {
      if (!set || !Array.isArray(set.keys)) throw new SigningError('a key set is {"keys": [...]}');
      if (set.keys.some((e) => e && typeof e === "object" && "private_key" in e)) throw new SigningError("the key set contains a private key; publish only public keys");
      for (const entry of set.keys) ring.add(PublicKey.fromEntry(entry));
    }
    return ring;
  }
}

/** An issuer's private Ed25519 key. Hold it where records are sealed, nowhere else. */
export class SigningKey {
  constructor(issuer, rawPrivate) {
    if (typeof issuer !== "string" || !issuer) throw new SigningError("issuer must be a non-empty string");
    if (!(rawPrivate instanceof Uint8Array) || rawPrivate.length !== 32) throw new SigningError("an Ed25519 private key is 32 bytes");
    this.issuer = issuer;
    this._key = createPrivateKey({ key: Buffer.concat([PKCS8_PREFIX, Buffer.from(rawPrivate)]), format: "der", type: "pkcs8" });
    const spki = createPublicKey(this._key).export({ format: "der", type: "spki" });
    this.public = new PublicKey(issuer, spki.subarray(spki.length - 32));
  }

  static fromPrivateBytes(issuer, raw) {
    return new SigningKey(issuer, raw);
  }

  get keyId() {
    return this.public.keyId;
  }

  sign(message) {
    return edSign(null, Buffer.from(message, "utf8"), this._key).toString("base64");
  }

  /** The `seal` fields a signing store adds: key_id and signature. */
  signSeal(hash) {
    return { key_id: this.keyId, signature: this.sign(SEAL_CONTEXT + hash) };
  }
}

/**
 * Check a sealed record's issuer signature against `seal.hash`. Returns `{ ok: true, issuer }` or
 * `{ ok: false, reason }`. Whether the hash matches the body is the chain check's job; both are needed.
 */
export function verifySeal(record, keyring) {
  const seal = record?.seal ?? {};
  if (!seal.signature) return { ok: false, reason: "unsigned" };
  if (!seal.key_id) return { ok: false, reason: "signature without key_id" };
  const key = keyring.get(seal.key_id);
  if (!key) return { ok: false, reason: `signed by unknown key ${seal.key_id}` };
  let valid, why;
  try {
    [valid, why] = key.validAt(record.timestamp ?? "");
  } catch (err) {
    return { ok: false, reason: err.message };
  }
  if (!valid) return { ok: false, reason: why };
  if (!key.verify(SEAL_CONTEXT + String(seal.hash ?? ""), seal.signature)) return { ok: false, reason: `signature does not verify under ${seal.key_id}` };
  return { ok: true, issuer: key.issuer };
}
