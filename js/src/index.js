/**
 * Warrant: the decision ledger for AI agents.
 *
 * The JavaScript SDK records decisions to a Warrant collector: `Warrant`, `decide()`,
 * evidence, cost, outcomes and human verdicts, with client-side redaction and
 * background delivery that spills to disk while the collector is unreachable, and the
 * Agent Decision Record's evidence rules, warrant check and lifecycle. The local store,
 * replay and import live in the Python package; see https://warrantai.dev.
 */

export { SCHEMA_VERSION, VERSION, ValidationError, loadSchema, validate } from "./schema.js";
export { CitationError, Decision, EVIDENCE_TYPES, HUMAN_VERDICTS, LIFECYCLE_STATES, MANDATE_RESULTS, NotWarranted, Verdict, Warrant, WarrantState, currentDecision } from "./client.js";
export { AUTHORISING, EDGES, REASONS, STATES, TERMINAL, admit, assess, checkHistory, checkTransition, deriveState, humanLinked, legal, recordDigests, selfAttested } from "./admissibility.js";
export { Emitter, PermanentSinkError, SinkError } from "./emit.js";
export { HttpSink } from "./sinks.js";
export { Redactor, TEXT_FIELDS } from "./redaction.js";
export { SALT_BYTES, canonicalJson, contentBytes, contentHash, recordHash, saltedHash } from "./hashing.js";
export { CHECKPOINT_CONTEXT, Keyring, PublicKey, SEAL_CONTEXT, SigningError, SigningKey, keyIdFor, sealMessage, verifySeal } from "./signing.js";
export { consistencyProof, inclusionProof, merkleRoot, verifyConsistency, verifyInclusion } from "./merkle.js";
export { deterministicUlid, ulid } from "./ids.js";
