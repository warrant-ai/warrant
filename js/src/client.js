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
import { randomBytes } from "node:crypto";
import { basename, extname, join } from "node:path";
import { Emitter } from "./emit.js";
import { SALT_BYTES, contentHash, recordHash, saltedHash } from "./hashing.js";
import { verifySeal } from "./signing.js";
import { AUTHORISING, STATES, assess, checkTransition, legal } from "./admissibility.js";
import { ulid } from "./ids.js";
import { SCHEMA_VERSION, ValidationError, validate } from "./schema.js";
import { HttpSink } from "./sinks.js";

export const MANDATE_RESULTS = ["allow", "deny", "escalate", "unchecked"];
export const EVIDENCE_TYPES = ["model_call", "tool_call", "document", "web", "other", "record", "mandate", "attestation", "human_review"];
export const LIFECYCLE_STATES = ["proposed", "pending_evidence", "escalated", "warranted", "refused", "committed"];
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
  /**
   * `obligations` are the obligation records that apply to this decision (ADR 1.4), `enforce` says
   * acting without a warrant must fail closed, and `retention` is `{ class, seconds? }`.
   */
  constructor(result, { policyId, policyVersion, clause, reason, flagged = false, obligations = [], enforce = false, retention } = {}) {
    if (!MANDATE_RESULTS.includes(result)) throw new RangeError(`mandate result must be one of ${MANDATE_RESULTS.join(", ")}, got ${JSON.stringify(result)}`);
    this.result = result;
    this.policyId = policyId;
    this.policyVersion = policyVersion;
    this.clause = clause;
    this.reason = reason;
    this.flagged = Boolean(flagged);
    // Not enumerable, so a verdict compares and serialises exactly as it did before 0.7.1.
    Object.defineProperties(this, {
      obligations: { value: Object.freeze(obligations.map((o) => Object.freeze({ ...o }))), enumerable: false },
      enforce: { value: Boolean(enforce), enumerable: false },
      retention: { value: retention ? Object.freeze({ ...retention }) : undefined, enumerable: false },
    });
    Object.freeze(this);
  }

  /** True for `allow` only. `unchecked` is not allowed: a policy engine has to say so. */
  get allowed() {
    return this.result === "allow";
  }
}

/** An upstream record could not be cited: unsealed, altered, or its signature does not verify. */
export class CitationError extends Error {
  constructor(message) {
    super(message);
    this.name = "CitationError";
  }
}

/**
 * A decision tried to act without a warrant: fail closed (ADR level 2). `state` is the lifecycle
 * state it reached instead, and `unmet` the verifiable obligations no admitted evidence satisfied.
 */
export class NotWarranted extends Error {
  constructor(recordId, state, unmet) {
    const detail = unmet.length ? `; unmet: ${unmet.join(", ")}` : "";
    super(`decision ${recordId} is ${state ?? "not assessed"}, not warranted${detail}`);
    this.name = "NotWarranted";
    this.recordId = recordId;
    this.state = state;
    this.unmet = [...unmet];
  }
}

/** What `Decision.warrant()` found: the state reached, and why. */
export class WarrantState {
  constructor(state, met, unmet, rejected) {
    this.state = state;
    this.met = Object.freeze([...met]);
    this.unmet = Object.freeze([...unmet]);
    /** `[evidenceName, reasonCode]` for every item the rules rejected. */
    this.rejected = Object.freeze(rejected.map((r) => Object.freeze([...r])));
    Object.freeze(this);
  }

  get warranted() {
    return AUTHORISING.includes(this.state);
  }
}

function nowIso() {
  return new Date().toISOString();
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
    this._blobs = {};
    this._claims = [];
    this._parents = [];
    this._retention = undefined;
    this._obligations = [];
    this._warrantAt = null;
    this._openedAt = nowIso();
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
    const known = new Set(this._obligations.map((o) => o.id));
    for (const ob of this._verdict.obligations ?? []) {
      if (!known.has(ob.id)) this._obligations.push({ ...ob });
    }
    if (this._verdict.retention && this._retention === undefined) this._applyPolicyRetention(this._verdict.retention);
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

  /**
   * Attach evidence by reference. Content is hashed here and never stored. Returns the hash.
   *
   * `provider` names who produced it; `obligation` is the obligation id it is offered against.
   * `sensitive` uses a salted digest (ADR 4): the salt rides to the collector's sidecar, never
   * onto the record, so erasing the sidecar entry unlinks the digest while the record still verifies.
   */
  evidence(name, { uri, type = "other", content, contentHash: given, excerpt, retrievedAt, provider, obligation, sensitive = false } = {}) {
    this._assertOpen();
    requireText(name, "evidence name");
    requireText(uri, "evidence uri");
    if (!EVIDENCE_TYPES.includes(type)) throw new RangeError(`evidence type must be one of ${EVIDENCE_TYPES.join(", ")}, got ${JSON.stringify(type)}`);
    if (content === undefined && given === undefined) throw new TypeError("evidence needs either content (hashed locally) or contentHash");
    if (given !== undefined && !SHA256_RE.test(given)) throw new TypeError("contentHash must be 64 lowercase hex characters (sha256)");
    if (provider !== undefined) requireText(provider, "provider");
    if (obligation !== undefined) requireText(obligation, "obligation");
    let digest;
    if (sensitive) {
      if (content === undefined) throw new TypeError("sensitive evidence needs its content: a salted digest cannot be made from a bare hash");
      if (excerpt !== undefined) throw new TypeError("sensitive evidence cannot carry an excerpt; the excerpt would be the personal data in clear");
      const salt = randomBytes(SALT_BYTES);
      digest = saltedHash(content, salt);
      this._blobs[digest] = { salt: salt.toString("hex") };
    } else {
      digest = given ?? contentHash(content);
    }
    const item = { name, type, uri, content_hash: digest };
    if (sensitive) item.salted = true;
    if (provider !== undefined) item.provider = provider;
    if (obligation !== undefined) item.obligation = obligation;
    if (retrievedAt !== undefined) item.retrieved_at = asTimestamp(retrievedAt);
    if (excerpt !== undefined) {
      if (typeof excerpt !== "string") throw new TypeError("excerpt must be a string");
      item.excerpt = excerpt;
    }
    this._evidence.push(item);
    return digest;
  }

  /** The hex salt behind a sensitive item's digest, for a caller that keeps its own sidecar. */
  saltFor(digest) {
    return this._blobs[digest]?.salt;
  }

  // -- Agent Decision Record: claims, handoffs, retention ------------------------

  /** Record what the agent asserts. A claim is never evidence, here or downstream (ADR 1.2). */
  claim(claim, value) {
    this._assertOpen();
    requireText(claim, "claim");
    const item = { claim };
    if (value !== undefined) {
      try {
        item.value = JSON.parse(JSON.stringify(value));
      } catch (err) {
        throw new TypeError(`claim value must be JSON-serialisable: ${err.message}`);
      }
    }
    this._claims.push(item);
  }

  /**
   * Rely on an upstream agent's sealed record, possibly another organisation's (ADR rule 5). The
   * parent is cited by id and sealed hash; its claims are never copied. With `keyring` its issuer
   * signature must verify. `state` overrides the parent's own verdict state. Returns the cited hash.
   */
  cite(parent, { keyring, name, obligation, state } = {}) {
    this._assertOpen();
    const seal = parent && typeof parent === "object" ? parent.seal : undefined;
    if (!seal || typeof seal !== "object" || !seal.hash) throw new CitationError("the parent is not sealed; cite a record exported from its issuer's store");
    if (recordHash(parent, seal.prev_hash) !== seal.hash) throw new CitationError(`parent ${parent.record_id} does not match its own seal; it was altered`);
    let issuer = parent.tenant;
    if (keyring) {
      const result = verifySeal(parent, keyring);
      if (!result.ok) throw new CitationError(`parent ${parent.record_id}: ${result.reason}`);
      issuer = result.issuer;
    }
    if (state !== undefined && !LIFECYCLE_STATES.includes(state)) throw new RangeError(`state must be one of ${LIFECYCLE_STATES.join(", ")}`);
    const citedState = state ?? parent.verdict?.state;
    const entry = { record_id: parent.record_id, hash: seal.hash };
    if (issuer) entry.issuer = issuer;
    if (seal.key_id) entry.key_id = seal.key_id;
    if (citedState) entry.state = citedState;
    this._parents.push(entry);
    this.evidence(name ?? `${issuer ?? "upstream"}:${parent.decision?.class ?? "record"}`, {
      uri: `adr://${issuer ?? "unknown"}/${parent.record_id}#${seal.hash}`,
      type: "record",
      contentHash: seal.hash,
      // Only a verified signature names a provider; an unauthenticated citation cannot authorise.
      provider: keyring ? issuer || undefined : undefined,
      obligation,
      retrievedAt: parent.timestamp,
    });
    const link = { record_id: parent.record_id, hash: seal.hash };
    if (issuer) link.issuer = issuer;
    this._evidence[this._evidence.length - 1].parent = link;
    return seal.hash;
  }

  /** Record the retention duty this decision falls under. The store never deletes. */
  retention(retentionClass, { retainUntil, legalHold = false } = {}) {
    this._assertOpen();
    requireText(retentionClass, "retention class");
    const item = { class: retentionClass };
    if (retainUntil !== undefined) item.retain_until = asTimestamp(retainUntil);
    if (legalHold) item.legal_hold = true;
    this._retention = item;
  }

  _applyPolicyRetention(policyRetention) {
    const item = { class: policyRetention.class };
    if (policyRetention.seconds) item.retain_until = new Date(Date.parse(this._openedAt) + policyRetention.seconds * 1000).toISOString();
    this._retention = item;
  }

  // -- the warrant (ADR level 2) -----------------------------------------------

  /** Declare an obligation by hand. Policy bundles normally supply these through `check()`. */
  obligation(obligationId, { requires, kind = "verifiable", providers = [], maxAgeSeconds, name, clause } = {}) {
    this._assertOpen();
    requireText(obligationId, "obligation id");
    if (this._obligations.some((o) => o.id === obligationId)) throw new Error(`obligation ${JSON.stringify(obligationId)} is already declared`);
    if (!EVIDENCE_TYPES.includes(requires)) throw new RangeError(`requires must be one of ${EVIDENCE_TYPES.join(", ")}, got ${JSON.stringify(requires)}`);
    if (!["verifiable", "advisory"].includes(kind)) throw new RangeError("kind must be verifiable or advisory");
    if (!Array.isArray(providers)) throw new TypeError("providers must be a list of provider names");
    if (providers.includes("self")) throw new RangeError("'self' cannot be a qualified provider");
    if (maxAgeSeconds !== undefined && !(Number.isInteger(maxAgeSeconds) && maxAgeSeconds >= 0)) throw new RangeError("maxAgeSeconds must be a non-negative integer");
    const ob = { id: obligationId, requires, kind };
    if (providers.length) ob.providers = [...providers];
    if (maxAgeSeconds !== undefined) ob.max_age_seconds = maxAgeSeconds;
    if (name !== undefined) ob.name = name;
    if (clause !== undefined) ob.clause = clause;
    this._obligations.push(ob);
  }

  /** Apply the admissibility rules to the evidence so far and report the state reached. */
  warrant() {
    this._assertOpen();
    this._warrantAt = nowIso();
    this._autoOffer();
    const draft = this._compose("withheld", this._action ?? "none", this._summary, { final: false });
    const assessment = assess(draft, { at: this._warrantAt });
    const rejected = Object.keys(assessment.admissions)
      .map(Number)
      .sort((a, b) => a - b)
      .filter((i) => assessment.admissions[i].status === "rejected")
      .map((i) => [draft.evidence[i].name ?? String(i), assessment.admissions[i].reason ?? ""]);
    return new WarrantState(assessment.state, assessment.met, assessment.unmet, rejected);
  }

  /** Act only on a warrant. Throws `NotWarranted` instead of recording the action. */
  commit(action, options = {}) {
    const state = this.warrant();
    if (!state.warranted) throw new NotWarranted(this.recordId, state.state, state.unmet);
    this._act(action, options);
  }

  _enforced() {
    return Boolean(this._client.enforce || (this._verdict && this._verdict.enforce));
  }

  /**
   * Offer an unassigned item to the one obligation it can only be meant for: by the obligation's
   * `name` first, then by type when exactly one obligation requires that type. Written onto the
   * record, so a verifier judges the same pairing.
   */
  _autoOffer() {
    for (const item of this._evidence) {
      if ("obligation" in item || item.type === "model_call") continue;
      const byName = this._obligations.filter((o) => o.name && o.name === item.name);
      const byType = this._obligations.filter((o) => !o.name && o.requires === item.type);
      const match = byName.length ? byName : byType;
      if (match.length === 1) item.obligation = match[0].id;
    }
  }

  /** Obligations with met and unmet, admissions on the evidence, and the verdict (ADR 1.4, 2, 3). */
  _attachVerdict(record, verdict) {
    record.obligations = this._obligations.map((o) => ({ ...o }));
    const at = this._warrantAt ?? nowIso();
    record.verdict = { state: "proposed", at };
    const assessment = assess(record, { at });
    record.obligations = assessment.obligations;
    for (const [index, admission] of Object.entries(assessment.admissions)) record.evidence[Number(index)].admission = admission;
    const out = { state: assessment.state ?? "proposed", decided_by: verdict.policyId ? `policy:${verdict.policyId}@${verdict.policyVersion}` : "rules:adr/0.2", at };
    if (assessment.met.length) out.met = assessment.met;
    if (assessment.unmet.length) out.unmet = assessment.unmet;
    out.history = assessment.history.map((state) => ({ state, at }));
    record.verdict = out;
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

  /**
   * Record that the action was taken. Call it once, after the action succeeds. Where the policy
   * for this class sets `enforce: true`, or the client was created with `enforce: true`, acting
   * without a warrant throws `NotWarranted` (fail closed).
   */
  act(action, options = {}) {
    if (this._enforced()) {
      const state = this.warrant();
      if (!state.warranted) throw new NotWarranted(this.recordId, state.state, state.unmet);
    }
    this._act(action, options);
  }

  _act(action, { summary, costCentre, alternatives } = {}) {
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
    if (this._obligations.length) this._autoOffer();
    return this._compose(status, action, summary, { final: true });
  }

  _compose(status, action, summary, { final }) {
    const client = this._client;
    const decision = { class: this.decisionClass, action, subject: this.subject, status };
    if (summary) decision.summary = summary;
    if (this._alternatives.length) decision.alternatives = this._alternatives;
    if (this._inputs !== undefined) decision.inputs = this._inputs;
    if (this._claims.length) decision.claims = [...this._claims];

    const verdict = this._verdict ?? new Verdict("unchecked");
    const mandate = { result: verdict.result };
    if (verdict.policyId) mandate.policy_id = verdict.policyId;
    if (verdict.policyVersion) mandate.policy_version = verdict.policyVersion;
    if (verdict.clause) mandate.clause = verdict.clause;
    if (verdict.reason) mandate.reason = verdict.reason;
    if (verdict.flagged) mandate.flagged = true;

    const actor = { name: client.agent.name, version: client.agent.version };
    if (client.agent.instance) actor.instance = client.agent.instance;
    if (client.agent.model) actor.model = client.agent.model;
    if (client.agent.runtime) actor.runtime = client.agent.runtime;
    if (client.agent.identity) actor.identity = { ...client.agent.identity };
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
      evidence: this._evidence.map((e) => ({ ...e })),
      human: { ...this._human },
      cost,
      outcome: { status: "pending" },
    };
    if (this._parents.length) record.parents = this._parents.map((p) => ({ ...p }));
    if (this._retention) record.retention = { ...this._retention };
    if (this._obligations.length || this._warrantAt) this._attachVerdict(record, verdict);
    if (!final) return record;
    const out = client.redactor ? client.redactor.apply(record) : record;
    // The sidecar travels with the record to the collector, which stores it apart and strips it
    // before sealing; it never becomes part of the sealed record.
    if (Object.keys(this._blobs).length) out._blobs = { ...this._blobs };
    return out;
  }
}

let warnedDefaultAgent = false;

function agentFrom(agent, logger) {
  let name = agent?.name ?? process.env.WARRANT_AGENT_NAME;
  let version = agent?.version ?? process.env.WARRANT_AGENT_VERSION;
  const instance = agent?.instance ?? process.env.WARRANT_AGENT_INSTANCE;
  let identity;
  if (agent?.identity !== undefined) {
    const [registry, id] = Array.isArray(agent.identity) ? agent.identity : [agent.identity?.registry, agent.identity?.id];
    identity = { registry: requireText(registry, "agent.identity.registry"), id: requireText(id, "agent.identity.id") };
    if (!Array.isArray(agent.identity) && agent.identity.uri) identity.uri = agent.identity.uri;
  }
  if (!name || !version) {
    name = name || (process.argv[1] ? basename(process.argv[1], extname(process.argv[1])) : "unnamed-agent");
    version = version || "0";
    if (!warnedDefaultAgent) {
      logger.warn(`warrant: agent identity not set; recording as ${name}@${version}. Pass agent: { name, version } or set WARRANT_AGENT_NAME/VERSION`);
      warnedDefaultAgent = true;
    }
  }
  return { name: requireText(name, "agent.name"), version: requireText(version, "agent.version"), instance, model: agent?.model, runtime: agent?.runtime, identity };
}

/** Entry point. One instance per stream; share it across decisions. */
export class Warrant {
  constructor(stream, { tenant, store, token, agent, onBehalfOf, policy, redact, currency, captureInputs = false, enforce = false, spillDir, maxQueue, batchSize, flushIntervalMs, logger = console } = {}) {
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
    this.enforce = Boolean(enforce);
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
      const { _blobs, ...body } = record;
      validate(body);
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

  /**
   * Append a lifecycle transition for an earlier decision (ADR 3). History is never edited.
   *
   * The JavaScript SDK has no store to read the decision back from, so the caller passes the state
   * being left (`fromState`) and, for the checks that depend on it, the decision record itself
   * (`decision`): leaving `escalated` or `pending_evidence` for `warranted` needs a named
   * `reviewer` linked by digest to what they were `shown`, and a transition can never supply
   * missing evidence. Returns the new record id.
   */
  transition(decisionRecordId, toState, { decidedBy, fromState, decision, reviewer, shown, reason, recordId } = {}) {
    if (!STATES.includes(toState)) throw new RangeError(`toState must be one of ${STATES.join(", ")}, got ${JSON.stringify(toState)}`);
    if (typeof decidedBy !== "string" || !decidedBy) throw new TypeError("decidedBy must name who decided, e.g. human:a.rao or policy:CR-07@2026.4");
    if (fromState === undefined || fromState === null) throw new TypeError("pass fromState: there is no local store to read the current state from");
    if (!STATES.includes(fromState)) throw new RangeError(`fromState must be one of ${STATES.join(", ")}, got ${JSON.stringify(fromState)}`);
    if (!legal(fromState, toState)) throw new Error(`illegal transition ${fromState} -> ${toState}`);
    let human;
    if (reviewer !== undefined || shown !== undefined) {
      requireText(reviewer, "a human transition needs the reviewer's name: reviewer");
      const list = [...(shown ?? [])];
      if (list.some((d) => typeof d !== "string" || !SHA256_RE.test(d))) throw new TypeError("shown must be sha256 digests of the material the reviewer saw");
      human = { required: true, reviewer, shown: list, at: nowIso(), verdict: toState === "refused" ? "reject" : "approve" };
    }
    const verdict = { state: toState, from_state: fromState, decided_by: decidedBy, at: nowIso() };
    if (reason) verdict.reason = reason;
    const section = { verdict };
    if (human) section.human = human;
    if (decision) {
      if (decision.record_id && decision.record_id !== decisionRecordId) throw new Error(`the decision given is ${decision.record_id}, not ${decisionRecordId}`);
      const problem = checkTransition(decision, fromState, section);
      if (problem) throw new Error(problem);
    } else if (toState === "warranted" && fromState === "escalated" && !human) {
      throw new Error("leaving escalated for warranted needs a named reviewer and the digests they were shown");
    } else if (toState === "warranted" && fromState === "pending_evidence") {
      throw new Error("leaving pending_evidence for warranted needs the decision record, to check that only a person was missing: pass decision");
    }
    return this._appendLinked("transition", decisionRecordId, section, recordId);
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
