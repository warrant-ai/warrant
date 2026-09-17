/** Client-side redaction applied to a record before it leaves the process. */

// Free-text fields that patterns are applied to. Structural fields (ids, hashes, URIs,
// timestamps, class names) are never touched by patterns because a broad pattern would
// corrupt them; use `fields` to drop those outright.
export const TEXT_FIELDS = ["decision.summary", "decision.alternatives[]", "evidence[].excerpt", "human.note"];

function parsePath(path) {
  if (typeof path !== "string" || !path || path.startsWith(".") || path.endsWith(".")) {
    throw new TypeError(`invalid field path: ${JSON.stringify(path)}`);
  }
  return path.split(".");
}

/** Apply `fn` at `path` below `node`. `seg[]` means every element of an array. */
function walk(node, path, fn, removeNonStrings) {
  if (!path.length || node === null || typeof node !== "object" || Array.isArray(node)) return;
  const [seg, ...rest] = path;
  const isList = seg.endsWith("[]");
  const key = isList ? seg.slice(0, -2) : seg;
  if (!Object.hasOwn(node, key)) return;
  if (isList) {
    const items = node[key];
    if (!Array.isArray(items)) return;
    if (rest.length) {
      for (const item of items) walk(item, rest, fn, removeNonStrings);
    } else {
      node[key] = items.filter((v) => typeof v === "string" || !removeNonStrings).map((v) => (typeof v === "string" ? fn(v) : v));
    }
    return;
  }
  if (rest.length) {
    walk(node[key], rest, fn, removeNonStrings);
  } else if (removeNonStrings && typeof node[key] !== "string") {
    delete node[key];
  } else {
    node[key] = fn(node[key]);
  }
}

/**
 * Redact records with regular expressions over the free-text fields and explicit field
 * paths. A string at a field path is replaced; any other value there is removed.
 */
export class Redactor {
  constructor({ patterns = [], fields = [], replacement = "[REDACTED]" } = {}) {
    this._patterns = [...patterns].map((p) => {
      const source = p instanceof RegExp ? p : new RegExp(p, "g");
      return source.global ? source : new RegExp(source.source, `${source.flags}g`);
    });
    this._fields = [...fields].map(parsePath);
    this._replacement = replacement;
  }

  /** Return a redacted deep copy; the input is not modified. */
  apply(record) {
    const out = structuredClone(record);
    if (this._patterns.length) {
      const scrub = (value) => (typeof value === "string" ? this._patterns.reduce((text, p) => text.replace(p, () => this._replacement), value) : value);
      for (const path of TEXT_FIELDS) walk(out, parsePath(path), scrub, false);
    }
    for (const path of this._fields) walk(out, path, () => this._replacement, true);
    return out;
  }
}
