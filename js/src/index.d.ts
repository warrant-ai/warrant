/** Warrant: the decision ledger for AI agents. Type declarations for the JavaScript SDK. */

export const VERSION: string;
export const SCHEMA_VERSION: "0";
export const MANDATE_RESULTS: readonly ["allow", "deny", "escalate", "unchecked"];
export const EVIDENCE_TYPES: readonly ["model_call", "tool_call", "document", "web", "other", "record", "mandate", "attestation", "human_review"];
export const LIFECYCLE_STATES: readonly ["proposed", "pending_evidence", "escalated", "warranted", "refused", "committed"];
export const HUMAN_VERDICTS: readonly ["approve", "reject", "amend"];
export const TEXT_FIELDS: readonly string[];

export type MandateResult = (typeof MANDATE_RESULTS)[number];
export type EvidenceType = (typeof EVIDENCE_TYPES)[number];
export type HumanVerdict = (typeof HUMAN_VERDICTS)[number];
export type LifecycleState = (typeof LIFECYCLE_STATES)[number];
export type CostKind = "model_call" | "tool_call" | "other";
export type Timestamp = string | Date;
/** A decision record as plain JSON. See `warrantai/schema` for the full shape. */
export type DecisionRecord = Record<string, unknown>;

export class ValidationError extends Error {
  readonly errors: string[];
}
/** Throws `ValidationError` listing every violation. */
export function validate(record: unknown): void;
export function loadSchema(): Record<string, unknown>;

export class Verdict {
  constructor(result: MandateResult, details?: { policyId?: string; policyVersion?: string; clause?: string; reason?: string; flagged?: boolean });
  readonly result: MandateResult;
  readonly policyId?: string;
  readonly policyVersion?: string;
  readonly clause?: string;
  readonly reason?: string;
  readonly flagged: boolean;
  /** True for `allow` only. `unchecked` is never allowed. */
  readonly allowed: boolean;
}

export interface PolicyEngine {
  /** Synchronous and in-process: this runs on the agent's path. */
  evaluate(decisionClass: string, inputs: Readonly<Record<string, unknown>>): Verdict;
}

export interface Sink {
  /** Persist a batch atomically, or throw. Throw `PermanentSinkError` when a retry cannot succeed. */
  write(records: DecisionRecord[]): void | Promise<void>;
}

export interface Logger {
  info(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

export interface AgentInfo {
  name: string;
  version: string;
  instance?: string;
  /** Model the agent ran on, as the runtime reported it. */
  model?: string;
  /** Agent runtime or framework, e.g. temporal, langgraph. */
  runtime?: string;
  /** The agent's entry in a registry the relying party recognises: `[registry, id]` or an object. */
  identity?: readonly [string, string] | { registry: string; id: string; uri?: string };
}

export interface EvidenceOptions {
  uri: string;
  type?: EvidenceType;
  /** Hashed locally and never stored. Bytes, a string, or anything JSON-serialisable. */
  content?: unknown;
  /** Use instead of `content` when you already hold the SHA-256. */
  contentHash?: string;
  /** Opt-in free text; redacted before it leaves the process. */
  excerpt?: string;
  retrievedAt?: Timestamp;
  /** Who produced it. The acting agent's own evidence is never admitted for its own obligation. */
  provider?: string;
  /** The obligation id this item is offered against. */
  obligation?: string;
  /** Salted digest (ADR 4). Needs `content`; refuses `excerpt` and a bare `contentHash`. */
  sensitive?: boolean;
}

/** An upstream record could not be cited: unsealed, altered, or its signature does not verify. */
export class CitationError extends Error {}

/**
 * Agent Decision Record support in JavaScript covers the record path: salted evidence, claims,
 * citations, retention and identity, plus the verifier primitives (`verifySeal`, the Merkle
 * functions). Obligations, the warrant check, fail-closed commit and lifecycle transitions are
 * Python-only in this release, like replay.
 */
export class Decision {
  readonly recordId: string;
  readonly decisionClass: string;
  readonly subject: string;
  readonly inputs: Record<string, unknown> | undefined;
  readonly verdict: Verdict | null;
  readonly acted: boolean;
  check(inputs?: Record<string, unknown>): Verdict;
  setInputs(inputs: Record<string, unknown>): void;
  /** Returns the content hash. */
  evidence(name: string, options: EvidenceOptions): string;
  modelCall(provider: string, model: string, options?: { tokensIn?: number; tokensOut?: number; amount?: number; uri?: string; content?: unknown; contentHash?: string; excerpt?: string }): string;
  toolCall(name: string, options?: { uri?: string; content?: unknown; contentHash?: string; amount?: number; provider?: string; excerpt?: string }): string;
  cost(amount: number, options?: { kind?: CostKind; provider?: string; model?: string; tokensIn?: number; tokensOut?: number }): void;
  /** Call once, after the action succeeds. */
  act(action: string, options?: { summary?: string; costCentre?: string; alternatives?: string[] }): void;
  requireHuman(options?: { reviewer?: string; note?: string }): void;
  /** The hex salt behind a sensitive item's digest. */
  saltFor(digest: string): string | undefined;
  /** What the agent asserts. Never evidence, here or downstream. */
  claim(claim: string, value?: unknown): void;
  /** Rely on an upstream sealed record by id and hash. Returns the cited hash. */
  cite(parent: DecisionRecord, options?: { keyring?: Keyring; name?: string; obligation?: string; state?: LifecycleState }): string;
  retention(retentionClass: string, options?: { retainUntil?: Timestamp; legalHold?: boolean }): void;
}

export interface WarrantOptions {
  tenant?: string;
  /** A collector URL, or any sink. Defaults to `$WARRANT_STORE`. */
  store?: string | Sink;
  /** Bearer token for the collector. Defaults to `$WARRANT_TOKEN`. */
  token?: string;
  agent?: AgentInfo;
  onBehalfOf?: string;
  policy?: PolicyEngine;
  redact?: Redactor;
  /** Three-letter ISO code. Defaults to `$WARRANT_CURRENCY`, then USD. */
  currency?: string;
  /** Store `check()` inputs on the record. For development and staging. */
  captureInputs?: boolean;
  spillDir?: string;
  maxQueue?: number;
  batchSize?: number;
  flushIntervalMs?: number;
  logger?: Logger;
}

export interface EmitterStats {
  submitted: number;
  delivered: number;
  spilled: number;
  recovered: number;
  failed_batches: number;
  dead: number;
  pending: number;
  queued: number;
}

export class Warrant {
  constructor(stream: string, options?: WarrantOptions);
  readonly stream: string;
  readonly tenant: string;
  readonly currency: string;
  readonly agent: AgentInfo;
  /** Runs `fn` as one decision scope and records it when `fn` settles. Rethrows what `fn` throws. */
  /** `recordId` lets a caller that may record the same event twice supply a deterministic ULID. */
  decide<T>(decisionClass: string, options: { subject: string; onBehalfOf?: string; alternatives?: string[]; recordId?: string }, fn: (decision: Decision) => T | Promise<T>): Promise<T>;
  /** Returns the new record id. */
  outcome(options: { label: string; decisionRecordId: string; observedAt?: Timestamp; score?: number; source?: string }): string;
  /** Returns the new record id. */
  humanVerdict(options: { reviewer: string; verdict: HumanVerdict; decisionRecordId: string; note?: string; at?: Timestamp; recordId?: string }): string;
  /** Resolves false on timeout. */
  flush(timeoutMs?: number): Promise<boolean>;
  close(timeoutMs?: number): Promise<void>;
  stats(): EmitterStats;
}

/** The innermost open decision in this async context. */
export function currentDecision(): Decision | undefined;

export class Redactor {
  constructor(options?: { patterns?: Iterable<string | RegExp>; fields?: Iterable<string>; replacement?: string });
  apply<T extends DecisionRecord>(record: T): T;
}

export class SinkError extends Error {}
export class PermanentSinkError extends SinkError {}

export class HttpSink implements Sink {
  constructor(url: string, token?: string, options?: { timeoutMs?: number; compress?: boolean; userAgent?: string; logger?: Pick<Logger, "warn">; fetch?: typeof fetch });
  readonly url: string;
  write(records: DecisionRecord[]): Promise<void>;
}

export class Emitter {
  constructor(sink: Sink, spillDir: string, options?: { maxQueue?: number; batchSize?: number; flushIntervalMs?: number; backoffInitialMs?: number; backoffMaxMs?: number; logger?: Logger });
  submit(record: DecisionRecord): void;
  flush(timeoutMs?: number): Promise<boolean>;
  close(timeoutMs?: number): Promise<void>;
  stats(): EmitterStats;
}

export function canonicalJson(value: unknown): string;
export function contentHash(content: unknown): string;
export function ulid(nowMs?: number): string;
/** ULID derived from `key`, so a repeated event gets the same id; same bytes as the Python SDK. */
export function deterministicUlid(tsMs: number, key: string): string;

export const SALT_BYTES: 32;
/** The bytes a content hash covers. */
export function contentBytes(content: unknown): Buffer;
/** ADR 4: SHA-256(salt || content), hex. `salt` must be 32 bytes. */
export function saltedHash(content: unknown, salt: Uint8Array): string;
/** The seal hash: every field but `seal`, canonical JSON, chained to `prevHash`. */
export function recordHash(record: DecisionRecord, prevHash: string | null | undefined): string;

export const SEAL_CONTEXT: "adr/0.2 seal\n";
export const CHECKPOINT_CONTEXT: "adr/0.2 checkpoint\n";
export class SigningError extends Error {}
export function keyIdFor(rawPublic: Uint8Array): string;

export interface KeyEntry {
  issuer: string;
  key_id?: string;
  alg?: "Ed25519";
  public_key: string;
  not_before?: string | null;
  revoked_at?: string | null;
}

export class PublicKey {
  constructor(issuer: string, raw: Uint8Array, options?: { notBefore?: string | null; revokedAt?: string | null });
  static fromEntry(entry: KeyEntry): PublicKey;
  readonly issuer: string;
  readonly keyId: string;
  readonly raw: Buffer;
  readonly notBefore: string | null;
  readonly revokedAt: string | null;
  toEntry(): Required<KeyEntry>;
  validAt(timestamp: string): [boolean, string];
  verify(message: string, signatureB64: string): boolean;
}

export class Keyring implements Iterable<PublicKey> {
  constructor(keys?: Iterable<PublicKey>);
  /** From parsed key-set documents. Refuses a set holding a private key. */
  static fromKeySets(...sets: { keys: KeyEntry[] }[]): Keyring;
  add(key: PublicKey): void;
  get(keyId: string): PublicKey | undefined;
  readonly size: number;
  [Symbol.iterator](): Iterator<PublicKey>;
}

export class SigningKey {
  constructor(issuer: string, rawPrivate: Uint8Array);
  static fromPrivateBytes(issuer: string, raw: Uint8Array): SigningKey;
  readonly issuer: string;
  readonly keyId: string;
  readonly public: PublicKey;
  sign(message: string): string;
  /** `sealedAt` (0.7.1 on) is signed with the hash; key validity is judged at sealing time. */
  signSeal(hash: string, sealedAt?: string): { key_id: string; signature: string; sealed_at?: string };
}

export type SealCheck = { ok: true; issuer: string } | { ok: false; reason: string };
/** What an issuer signs: context, hash, and `\n` + sealedAt when present (ADR 5.1). */
export function sealMessage(hash: string, sealedAt?: string | null): string;
/** Checks the issuer signature over `seal.hash` (and `sealed_at`); whether the hash matches the body is the chain check's job. */
export function verifySeal(record: DecisionRecord, keyring: Keyring): SealCheck;

/** RFC 9162 Merkle functions over hex leaf hashes, byte-identical to the Python reference. */
export function merkleRoot(entries: readonly string[]): string;
export function inclusionProof(index: number, entries: readonly string[]): string[];
export function verifyInclusion(entry: string, index: number, treeSize: number, proof: readonly string[], root: string): boolean;
export function consistencyProof(oldSize: number, entries: readonly string[]): string[];
export function verifyConsistency(oldSize: number, newSize: number, oldRoot: string, newRoot: string, proof: readonly string[]): boolean;
