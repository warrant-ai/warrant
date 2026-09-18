/**
 * Warrant: the decision ledger for AI agents.
 *
 * The JavaScript SDK records decisions to a Warrant collector: `Warrant`, `decide()`,
 * evidence, cost, outcomes and human verdicts, with client-side redaction and
 * background delivery that spills to disk while the collector is unreachable. The
 * local store, replay and import live in the Python package; see https://warrantai.dev.
 */

export { SCHEMA_VERSION, VERSION, ValidationError, loadSchema, validate } from "./schema.js";
export { Decision, EVIDENCE_TYPES, HUMAN_VERDICTS, MANDATE_RESULTS, Verdict, Warrant, currentDecision } from "./client.js";
export { Emitter, PermanentSinkError, SinkError } from "./emit.js";
export { HttpSink } from "./sinks.js";
export { Redactor, TEXT_FIELDS } from "./redaction.js";
export { canonicalJson, contentHash } from "./hashing.js";
export { deterministicUlid, ulid } from "./ids.js";
