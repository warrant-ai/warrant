/** Warrant: the decision ledger for AI agents. Type declarations for the JavaScript SDK. */

export const VERSION: string;
export const SCHEMA_VERSION: "0";
export const MANDATE_RESULTS: readonly ["allow", "deny", "escalate", "unchecked"];
export const EVIDENCE_TYPES: readonly ["model_call", "tool_call", "document", "web", "other"];
export const HUMAN_VERDICTS: readonly ["approve", "reject", "amend"];
export const TEXT_FIELDS: readonly string[];

export type MandateResult = (typeof MANDATE_RESULTS)[number];
export type EvidenceType = (typeof EVIDENCE_TYPES)[number];
export type HumanVerdict = (typeof HUMAN_VERDICTS)[number];
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
}

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
