// RFC 8785 canonical JSON, with no Node imports, so Temporal workflow code can bundle it. `hashing.js` re-exports it.

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
