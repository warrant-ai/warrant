/**
 * Warrant: the decision ledger for AI agents.
 *
 * This release ships the decision record schema v0 and a validator. The SDK's
 * decide() wrapper, the policy check, the local store and replay arrive in the
 * following releases; see https://warrantai.dev.
 */

import { createRequire } from "node:module";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const require = createRequire(import.meta.url);

export const VERSION = require("../package.json").version;
export const SCHEMA_VERSION = "0";

const schema = require(`../schema/decision-record.v${SCHEMA_VERSION}.json`);

// strictRequired is off because the conditional `required` lists in allOf/if/then
// refer to properties declared at the root, which Ajv's heuristic cannot see.
const ajv = new Ajv2020({ allErrors: true, strict: true, strictRequired: false });
addFormats(ajv);
const compiled = ajv.compile(schema);

/** A record does not conform to the decision record schema. `errors` lists every violation. */
export class ValidationError extends Error {
  constructor(errors) {
    super(errors.join("; "));
    this.name = "ValidationError";
    this.errors = errors;
  }
}

/** Return the decision record JSON Schema shipped with this package. */
export function loadSchema() {
  return structuredClone(schema);
}

function describe(error) {
  const path = error.instancePath ? error.instancePath.slice(1) : "(root)";
  const detail =
    error.keyword === "additionalProperties"
      ? `${error.message}: ${error.params.additionalProperty}`
      : error.message;
  return `${path}: ${detail}`;
}

/**
 * Throw `ValidationError` listing every violation, or return undefined if valid.
 * `record` is the parsed JSON object, not a string.
 */
export function validate(record) {
  if (record === null || typeof record !== "object" || Array.isArray(record)) {
    const got = record === null ? "null" : Array.isArray(record) ? "array" : typeof record;
    throw new ValidationError([`(root): expected an object, got ${got}`]);
  }
  if (compiled(record)) return;
  const errors = compiled.errors
    .map(describe)
    .sort((a, b) => a.localeCompare(b));
  throw new ValidationError(errors);
}
