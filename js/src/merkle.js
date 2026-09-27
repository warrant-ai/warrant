/**
 * RFC 9162 Merkle trees over a chain's record hashes: roots, inclusion and consistency proofs.
 * Byte-identical to the Python reference (warrant.merkle). Entries and proofs are hex strings.
 */

import { createHash } from "node:crypto";

const sha = (...parts) => createHash("sha256").update(Buffer.concat(parts)).digest();
const leafHash = (entry) => sha(Buffer.from([0]), entry);
const nodeHash = (left, right) => sha(Buffer.from([1]), left, right);

function split(n) {
  let k = 1;
  while (k * 2 < n) k *= 2;
  return k;
}

function toBuffers(entriesHex) {
  return entriesHex.map((h, i) => {
    if (typeof h !== "string" || !/^[0-9a-f]{64}$/.test(h)) throw new TypeError(`leaf ${i} is not a 32-byte hex hash`);
    return Buffer.from(h, "hex");
  });
}

function rootOf(entries) {
  const n = entries.length;
  if (n === 0) return sha(Buffer.alloc(0));
  if (n === 1) return leafHash(entries[0]);
  const k = split(n);
  return nodeHash(rootOf(entries.slice(0, k)), rootOf(entries.slice(k)));
}

function pathOf(index, entries) {
  const n = entries.length;
  if (n === 1) return [];
  const k = split(n);
  if (index < k) return [...pathOf(index, entries.slice(0, k)), rootOf(entries.slice(k))];
  return [...pathOf(index - k, entries.slice(k)), rootOf(entries.slice(0, k))];
}

function subproof(m, entries, complete) {
  const n = entries.length;
  if (m === n) return complete ? [] : [rootOf(entries)];
  const k = split(n);
  if (m <= k) return [...subproof(m, entries.slice(0, k), complete), rootOf(entries.slice(k))];
  return [...subproof(m - k, entries.slice(k), false), rootOf(entries.slice(0, k))];
}

/** MTH(D[n]) as hex. */
export function merkleRoot(entriesHex) {
  return rootOf(toBuffers(entriesHex)).toString("hex");
}

/** PATH(m, D[n]): the audit path for the entry at `index`. */
export function inclusionProof(index, entriesHex) {
  const entries = toBuffers(entriesHex);
  if (!Number.isInteger(index) || index < 0 || index >= entries.length) throw new RangeError(`index ${index} is outside a tree of size ${entries.length}`);
  return pathOf(index, entries).map((b) => b.toString("hex"));
}

/** RFC 9162 section 2.1.3.2. */
export function verifyInclusion(entryHex, index, treeSize, proofHex, rootHex) {
  if (!(index >= 0 && index < treeSize)) return false;
  let fn = index;
  let sn = treeSize - 1;
  let r = leafHash(Buffer.from(entryHex, "hex"));
  for (const p of proofHex.map((h) => Buffer.from(h, "hex"))) {
    if (sn === 0) return false;
    if (fn & 1 || fn === sn) {
      r = nodeHash(p, r);
      if (!(fn & 1)) {
        while (fn && !(fn & 1)) { fn >>= 1; sn >>= 1; }
      }
    } else {
      r = nodeHash(r, p);
    }
    fn >>= 1;
    sn >>= 1;
  }
  return sn === 0 && r.toString("hex") === rootHex;
}

/** PROOF(m, D[n]): shows the tree of the first `oldSize` entries is a prefix of this one. */
export function consistencyProof(oldSize, entriesHex) {
  const entries = toBuffers(entriesHex);
  if (!(oldSize > 0 && oldSize <= entries.length)) throw new RangeError(`old size ${oldSize} must be between 1 and ${entries.length}`);
  return subproof(oldSize, entries, true).map((b) => b.toString("hex"));
}

/** RFC 9162 section 2.1.4.2. */
export function verifyConsistency(oldSize, newSize, oldRootHex, newRootHex, proofHex) {
  if (oldSize < 1 || oldSize > newSize) return false;
  if (oldSize === newSize) return proofHex.length === 0 && oldRootHex === newRootHex;
  const path = proofHex.map((h) => Buffer.from(h, "hex"));
  if (!path.length) return false;
  if ((oldSize & (oldSize - 1)) === 0) path.unshift(Buffer.from(oldRootHex, "hex"));
  let fn = oldSize - 1;
  let sn = newSize - 1;
  while (fn & 1) { fn >>= 1; sn >>= 1; }
  let fr = path[0];
  let sr = path[0];
  for (const c of path.slice(1)) {
    if (sn === 0) return false;
    if (fn & 1 || fn === sn) {
      fr = nodeHash(c, fr);
      sr = nodeHash(c, sr);
      if (!(fn & 1)) {
        while (fn && !(fn & 1)) { fn >>= 1; sn >>= 1; }
      }
    } else {
      sr = nodeHash(sr, c);
    }
    fn >>= 1;
    sn >>= 1;
  }
  return fr.toString("hex") === oldRootHex && sr.toString("hex") === newRootHex && sn === 0;
}
