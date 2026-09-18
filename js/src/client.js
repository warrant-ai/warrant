/**
 * The Warrant client and the decide() scope.
 *
 *   const w = new Warrant("lending", { store: "https://collector.internal", agent: { name: "credit-underwriter", version: "2.3.1" } });
 *   await w.decide("credit.approve", { subject: loan.id }, async (d) => {
 *     const verdict = d.check({ amount: loan.amount, bureau_score: bureau.score });
 *     d.evidence("bureau_pull", { uri: bureau.uri, content: bureau.raw });
 *     if (verdict.allowed) d.act("approve", { summary });
 *   });
 *
 * Recording never blocks the caller: records are queued, delivered in batches in the
 * background, and spilled to disk while the collector is unreachable.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { basename, extname, join } from "node:path";
import { Emitter } from "./emit.js";
import { contentHash } from "./hashing.js";
import { ulid } from "./ids.js";
import { SCHEMA_VERSION, ValidationError, validate } from "./schema.js";
import { HttpSink } from "./sinks.js";

export const MANDATE_RESULTS = ["allow", "deny", "escalate", "unchecked"];
export const EVIDENCE_TYPES = ["model_call", "tool_call", "document", "web", "other"];
export const HUMAN_VERDICTS = ["approve", "reject", "amend"];
const COST_KINDS = ["model_call", "tool_call", "other"];
const CLASS_RE = /^[a-z0-9_]+(\.[a-z0-9_]+)*$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const CURRENCY_RE = /^[A-Z]{3}$/;

const current = new AsyncLocalStorage();

/** The innermost open decision in this async context, for integrations that attach evidence. */
export function currentDecision() {
  return current.getStore();
}

function requireText(value, label) {
  if (typeof value !== "string" || !value) throw new TypeError(`${label} must be a non-empty string`);
  return value;
}

function asTimestamp(value) {
  if (value === undefined || value === null) return new Date().toISOString();
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) throw new RangeError("timestamp is an invalid Date");
    return value.toISOString();
  }
  if (typeof value === "string" && value && !Number.isNaN(Date.parse(value))) return value;
  throw new TypeError(`not an ISO 8601 timestamp: ${JSON.stringify(value)}`);
}

/** Result of a mandate check. */
export class Verdict {
  constructor(result, { policyId, policyVersion, clause, reason, flagged = false } = {}) {
    if (!MANDATE_RESULTS.includes(result)) throw new RangeError(`mandate result must be one of ${MANDATE_RESULTS.join(", ")}, got ${JSON.stringify(result)}`);
    this.result = result;
    this.policyId = policyId;
    this.policyVersion = policyVersion;
    this.clause = clause;
    this.reason = reason;
    this.flagged = Boolean(flagged);
    Object.freeze(this);
  }

  /** True for `allow` only. `unchecked` is not allowed: a policy engine has to say so. */
  get allowed() {
    return this.result === "allow";
  }
}

/** One consequential action, opened by `Warrant.decide()` and closed when its callback settles. */
export class Decision {
  constructor(client, decisionClass, { subject, onBehalfOf, alternatives, recordId } = {}) {
    if (typeof decisionClass !== "string" || !CLASS_RE.test(decisionClass)) {
      throw new TypeError(`decision class must look like 'credit.approve', got ${JSON.stringify(decisionClass)}`);
    }
    if (recordId !== undefined && !(typeof recordId === "string" && ULID_RE.test(recordId))) {
      throw new TypeError(`recordId must be a 26-character ULID, got ${JSON.stringify(recordId)}`);
    }
    this._client = client;
    this.recordId = recordId ?? ulid();
    this.decisionClass = decisionClass;
    this.subject = requireText(subject, "subject");
    this._onBehalfOf = onBehalfOf;
    this._alternatives = alternatives ? [...alternatives] : [];
    this._actedAt = null;
    this._action = null;
    this._summary = undefined;
    this._costCentre = undefined;
    this._verdict = null;
    this._evidence = [];
    this._costItems = [];
    this._human = { required: false };
    this._inputs = undefined;
    this._closed = false;
  }

  // -- mandate ---------------------------------------------------------------

  /** Ask the policy engine whether this action is within mandate. Synchronous; runs in-process. */
  check(inputs = {}) {
    this._assertOpen();
    if (this._client.captureInputs && this._inputs === undefined) this.setInputs(inputs);
    const engine = this._client.policy;
    if (!engine) {
      this._verdict = new Verdict("unchecked", { reason: "no policy engine configured" });
    } else {
      const verdict = engine.evaluate(this.decisionClass, inputs);
      if (!(verdict instanceof Verdict)) throw new TypeError("policy.evaluate() must return a Verdict");
      this._verdict = verdict;
    }
    return this._verdict;
  }

  /** Record the inputs this decision is made on. Must be JSON-serialisable. */
  setInputs(inputs) {
    this._assertOpen();
    try {
      this._inputs = JSON.parse(JSON.stringify({ ...inputs }));
    } catch (err) {
      throw new TypeError(`inputs must be JSON-serialisable: ${err.message}`);
    }
  }

  get inputs() {
    return this._inputs;
  }

  get verdict() {
    return this._verdict;
  }

  get acted() {
    return this._action !== null;
  }

  // -- evidence and cost -----------------------------------------------------

  /** Attach evidence by reference. Content is hashed here and never stored. Returns the hash. */
  evidence(name, { uri, type = "other", content, contentHash: given, excerpt, retrievedAt } = {}) {
    this._assertOpen();
    requireText(name, "evidence name");
    requireText(uri, "evidence uri");
    if (!EVIDENCE_TYPES.includes(type)) throw new RangeError(`evidence type must be one of ${EVIDENCE_TYPES.join(", ")}, got ${JSON.stringify(type)}`);
    if (content === undefined && given === undefined) throw new TypeError("evidence needs either content (hashed locally) or contentHash");
    if (given !== undefined && !SHA256_RE.test(given)) throw new TypeError("contentHash must be 64 lowercase hex characters (sha256)");
    const digest = given ?? contentHash(content);
    const item = { name, type, uri, content_hash: digest };
    if (retrievedAt !== undefined) item.retrieved_at = asTimestamp(retrievedAt);
    if (excerpt !== undefined) {
      if (typeof excerpt !== "string") throw new TypeError("excerpt must be a string");
      item.excerpt = excerpt;
    }
    this._evidence.push(item);
    return digest;
  }

  /** Record one model call as evidence and as a cost line. */
  modelCall(provider, model, { tokensIn = 0, tokensOut = 0, amount = 0, uri, content, contentHash: given, excerpt } = {}) {
    this._assertOpen();
    requireText(provider, "provider");
    requireText(model, "model");
    const digest = this.evidence(`${provider}/${model}`, {
      uri: uri ?? `model://${provider}/${model}`,
      type: "model_call",
      content: content !== undefined || given !== undefined ? content : { provider, model, tokens_in: tokensIn, tokens_out: tokensOut },
      contentHash: given,
      excerpt,
    });
    this.cost(amount, { kind: "model_call", provider, model, tokensIn, tokensOut });
    return digest;
  }

  /** Record one tool call as evidence and, if it cost anything, as a cost line. */
  toolCall(name, { uri, content, contentHash: given, amount = 0, provider, excerpt } = {}) {
    this._assertOpen();
    const digest = this.evidence(name, { uri: uri ?? `tool://${name}`, type: "tool_call", content, contentHash: given, excerpt });
    if (amount) this.cost(amount, { kind: "tool_call", provider: provider ?? name });
    return digest;
  }

  cost(amount, { kind = "other", provider, model, tokensIn, tokensOut } = {}) {
    this._assertOpen();
    if (!COST_KINDS.includes(kind)) throw new RangeError(`cost kind must be one of ${COST_KINDS.join(", ")}, got ${JSON.stringify(kind)}`);
    if (typeof amount !== "number" || !Number.isFinite(amount) || amount < 0) throw new RangeError("cost amount must be a non-negative number");
    for (const [label, value] of [["tokensIn", tokensIn], ["tokensOut", tokensOut]]) {
      if (value !== undefined && (!Number.isInteger(value) || value < 0)) throw new RangeError(`${label} must be a non-negative integer`);
    }
    const item = { kind, amount };
    if (provider) item.provider = provider;
    if (model) item.model = model;
    if (tokensIn !== undefined) item.tokens_in = tokensIn;
    if (tokensOut !== undefined) item.tokens_out = tokensOut;
    this._costItems.push(item);
  }

  // -- action and human review -----------------------------------------------

  /** Record that the action was taken. Call it once, after the action succeeds. */
  act(action, { summary, costCentre, alternatives } = {}) {
    this._assertOpen();
    requireText(action, "action");
    if (this._action !== null) throw new Error(`act() already called on decision ${this.recordId} with ${JSON.stringify(this._action)}`);
    this._action = action;
    this._summary = summary;
    this._costCentre = costCentre;
    if (alternatives?.length) this._alternatives = [...alternatives];
    this._actedAt = new Date().toISOString();
  }

  /** Mark that a human must review this decision; the verdict arrives later via `Warrant.humanVerdict`. */
  requireHuman({ reviewer, note } = {}) {
    this._assertOpen();
    this._human = { required: true };
    if (reviewer) this._human.reviewer = reviewer;
    if (note) this._human.note = note;
  }

  _assertOpen() {
    if (this._closed) throw new Error(`decision ${this.recordId} is closed`);
  }

  _build(failure) {
    const client = this._client;
    let status, action, summary;
    if (failure !== undefined) {
      status = "failed";
      action = this._action ?? "none";
      // Only the error's class name: its message may carry personal data.
      summary = this._summary ?? `${failure?.constructor?.name ?? "Error"} raised before the action completed`;
    } else if (this._action !== null) {
      status = "acted";
      action = this._action;
      summary = this._summary;
    } else {
      status = "withheld";
      action = "none";
      summary = this._summary ?? (this._verdict ? `withheld: mandate result was ${this._verdict.result}` : "withheld");
    }

    const decision = { class: this.decisionClass, action, subject: this.subject, status };
    if (summary) decision.summary = summary;
    if (this._alternatives.length) decision.alternatives = this._alternatives;
    if (this._inputs !== undefined) decision.inputs = this._inputs;

    const verdict = this._verdict ?? new Verdict("unchecked");
    const mandate = { result: verdict.result };
    if (verdict.policyId) mandate.policy_id = verdict.policyId;
    if (verdict.policyVersion) mandate.policy_version = verdict.policyVersion;
    if (verdict.clause) mandate.clause = verdict.clause;
    if (verdict.reason) mandate.reason = verdict.reason;
    if (verdict.flagged) mandate.flagged = true;

    const actor = { name: client.agent.name, version: client.agent.version };
    if (client.agent.instance) actor.instance = client.agent.instance;
    const onBehalfOf = this._onBehalfOf ?? client.onBehalfOf;
    if (onBehalfOf) actor.on_behalf_of = onBehalfOf;

    const total = this._costItems.reduce((sum, item) => sum + item.amount, 0);
    const cost = { amount: Math.round(total * 1e6) / 1e6, currency: client.currency };
    if (this._costItems.length) cost.breakdown = this._costItems;
    if (this._costCentre) cost.cost_centre = this._costCentre;

    const record = {
      record_id: this.recordId,
      record_type: "decision",
      tenant: client.tenant,
      stream: client.stream,
      timestamp: this._actedAt ?? new Date().toISOString(),
      schema_version: SCHEMA_VERSION,
      origin: "live",
      actor,
      decision,
      mandate,
      evidence: this._evidence,
      human: this._human,
      cost,
      outcome: { status: "pending" },
    };
    return client.redactor ? client.redactor.apply(record) : record;
  }
}

let warnedDefaultAgent = false;

function agentFrom(agent, logger) {
  let name = agent?.name ?? process.env.WARRANT_AGENT_NAME;
  let version = agent?.version ?? process.env.WARRANT_AGENT_VERSION;
  const instance = agent?.instance ?? process.env.WARRANT_AGENT_INSTANCE;
  if (!name || !version) {
    name = name || (process.argv[1] ? basename(process.argv[1], extname(process.argv[1])) : "unnamed-agent");
    version = version || "0";
    if (!warnedDefaultAgent) {
      logger.warn(`warrant: agent identity not set; recording as ${name}@${version}. Pass agent: { name, version } or set WARRANT_AGENT_NAME/VERSION`);
      warnedDefaultAgent = true;
    }
  }
  return { name: requireText(name, "agent.name"), version: requireText(version, "agent.version"), instance };
}

/** Entry point. One instance per stream; share it across decisions. */
export class Warrant {
  constructor(stream, { tenant, store, token, agent, onBehalfOf, policy, redact, currency, captureInputs = false, spillDir, maxQueue, batchSize, flushIntervalMs, logger = console } = {}) {
    this.stream = requireText(stream, "stream");
    this.tenant = tenant ?? process.env.WARRANT_TENANT ?? "local";
    this.currency = currency ?? process.env.WARRANT_CURRENCY ?? "USD";
    if (!CURRENCY_RE.test(this.currency)) throw new RangeError(`currency must be a three-letter ISO code, got ${JSON.stringify(this.currency)}`);
    this.agent = agentFrom(agent, logger);
    this.onBehalfOf = onBehalfOf;
    if (policy && typeof policy.evaluate !== "function") throw new TypeError("policy must have an evaluate(decisionClass, inputs) method");
    this.policy = policy;
    this.redactor = redact;
    this.captureInputs = Boolean(captureInputs);
    this._log = logger;

    const target = store ?? process.env.WARRANT_STORE;
    let sink;
    if (typeof target === "string" && /^https?:\/\//.test(target)) {
      sink = new HttpSink(target, token, { logger });
    } else if (target && typeof target.write === "function") {
      sink = target;
    } else {
      throw new TypeError(
        "store must be a collector URL (https://...) or an object with write(records). " +
          "For local development run `warrant collector --store .warrant/records.db --insecure` from the Python package and point store at it.",
      );
    }
    this._emitter = new Emitter(sink, spillDir ?? join(".warrant", "spill"), { maxQueue, batchSize, flushIntervalMs, logger });
  }

  /**
   * Run `fn(decision)` as one decision scope and record it when `fn` settles: `acted` if
   * `act()` was called, `withheld` if not, `failed` if it threw (the error is rethrown).
   * Resolves to whatever `fn` returns.
   */
  async decide(decisionClass, options, fn) {
    if (typeof fn !== "function") throw new TypeError("decide(decisionClass, { subject }, fn) needs a function");
    const decision = new Decision(this, decisionClass, options);
    let result;
    let failure;
    let failed = false;
    try {
      result = await current.run(decision, () => fn(decision));
    } catch (err) {
      failed = true;
      failure = err ?? new Error("rejected without a reason");
    }
    decision._closed = true;
    const record = decision._build(failed ? failure : undefined);
    try {
      validate(record);
    } catch (err) {
      if (!(err instanceof ValidationError)) throw err;
      this._log.error(`warrant decision ${decision.recordId} produced an invalid record: ${err.errors.join("; ")}`);
      if (failed) throw failure;
      throw err;
    }
    this._emitter.submit(record);
    if (failed) throw failure;
    return result;
  }

  /** Append an outcome record linked to a past decision. Returns the new record id. */
  outcome({ label, decisionRecordId, observedAt, score, source } = {}) {
    requireText(label, "label");
    const outcome = { status: "observed", label, observed_at: asTimestamp(observedAt) };
    if (score !== undefined) {
      if (typeof score !== "number" || !Number.isFinite(score)) throw new TypeError("score must be a number");
      outcome.score = score;
    }
    if (source) outcome.source = source;
    return this._appendLinked("outcome", decisionRecordId, { outcome });
  }

  /** Append a human review verdict linked to a past decision. Returns the new record id. */
  humanVerdict({ reviewer, verdict, decisionRecordId, note, at, recordId } = {}) {
    if (!HUMAN_VERDICTS.includes(verdict)) throw new RangeError(`verdict must be one of ${HUMAN_VERDICTS.join(", ")}, got ${JSON.stringify(verdict)}`);
    requireText(reviewer, "reviewer");
    const human = { required: true, reviewer, verdict, at: asTimestamp(at) };
    if (note) human.note = note;
    return this._appendLinked("human_verdict", decisionRecordId, { human }, recordId);
  }

  _appendLinked(recordType, decisionRecordId, section, recordId) {
    if (typeof decisionRecordId !== "string" || !ULID_RE.test(decisionRecordId)) {
      throw new TypeError("decisionRecordId must be the record id of the decision (Decision.recordId)");
    }
    if (recordId !== undefined && !(typeof recordId === "string" && ULID_RE.test(recordId))) {
      throw new TypeError(`recordId must be a 26-character ULID, got ${JSON.stringify(recordId)}`);
    }
    let record = {
      record_id: recordId ?? ulid(),
      record_type: recordType,
      tenant: this.tenant,
      stream: this.stream,
      timestamp: new Date().toISOString(),
      schema_version: SCHEMA_VERSION,
      origin: "live",
      references: { decision_record_id: decisionRecordId },
      ...section,
    };
    if (this.redactor) record = this.redactor.apply(record);
    validate(record);
    this._emitter.submit(record);
    return record.record_id;
  }

  flush(timeoutMs) {
    return this._emitter.flush(timeoutMs);
  }

  stats() {
    return this._emitter.stats();
  }

  close(timeoutMs) {
    return this._emitter.close(timeoutMs);
  }
}
