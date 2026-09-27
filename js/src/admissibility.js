/**
 * ADR admissibility and lifecycle: which evidence counts, and what state a decision may reach.
 *
 * A line-for-line port of the Python SDK's `warrant.admissibility`. Pure functions over a record,
 * so the SDK that writes a verdict and any verifier that recomputes it cannot disagree about the
 * rules (ADR 2 and 3). `conformance/admissibility-cases.json` holds cases both implementations must
 * reproduce exactly, reason codes and messages included.
 *
 * The seven rules, applied in order to each item offered against an obligation; the first that
 * fails is the rejection reason: self_attested, unqualified_provider, stale / no_timestamp,
 * missing_digest, parent_not_cited / parent_not_warranted, wrong_type, and for a human obligation
 * unnamed_reviewer / material_not_linked.
 */

export const STATES = ["proposed", "pending_evidence", "escalated", "warranted", "refused", "committed"];
export const TERMINAL = ["refused", "committed"];
export const AUTHORISING = ["warranted", "committed"];

/** Legal lifecycle edges. `committed` is reachable only from `warranted`. */
export const EDGES = Object.freeze({
  proposed: ["pending_evidence", "escalated", "warranted", "refused"],
  pending_evidence: ["warranted", "refused", "escalated"],
  escalated: ["warranted", "refused"],
  warranted: ["committed", "refused"],
  refused: [],
  committed: [],
});

export const REASONS = [
  "self_attested", "unqualified_provider", "stale", "no_timestamp", "missing_digest",
  "parent_not_cited", "parent_not_warranted", "wrong_type", "unnamed_reviewer", "material_not_linked",
];

export function legal(fromState, toState) {
  return Object.hasOwn(EDGES, fromState) && EDGES[fromState].includes(toState);
}

/** Python's str() and repr() for the values these messages interpolate, so messages match exactly. */
function pyStr(value) {
  return value === null || value === undefined ? "None" : String(value);
}
function pyRepr(value) {
  if (value === null || value === undefined) return "None";
  if (typeof value !== "string") return String(value);
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  return quote + value.replace(/\\/g, "\\\\").replace(new RegExp(quote, "g"), `\\${quote}`) + quote;
}

/** A timestamp as epoch milliseconds, or null. A time with no zone is UTC, as in Python. */
export function parseTimestamp(value) {
  if (typeof value !== "string" || !value) return null;
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const ms = Date.parse(zoned);
  return Number.isNaN(ms) ? null : ms;
}

export function selfAttested(item, actorName) {
  const provider = item.provider;
  return !provider || provider === "self" || (actorName !== undefined && actorName !== null && provider === actorName);
}

/** Apply rules 1-6 to one item offered against one obligation. Returns `[admitted, reason]`. */
export function admit(item, obligation, { actorName, at, parents }) {
  if (selfAttested(item, actorName)) return [false, "self_attested"];
  const providers = obligation.providers || [];
  if (providers.length && !providers.includes(item.provider)) return [false, "unqualified_provider"];
  const maxAge = obligation.max_age_seconds;
  if (maxAge !== undefined && maxAge !== null) {
    const retrieved = parseTimestamp(item.retrieved_at);
    if (retrieved === null) return [false, "no_timestamp"];
    if (at !== null && at !== undefined && (at - retrieved) / 1000 > maxAge) return [false, "stale"];
  }
  if (!item.content_hash) return [false, "missing_digest"];
  if (item.type === "record") {
    const parent = item.parent || {};
    const cited = parents.get(parent.record_id ?? "");
    if (cited === undefined || cited.hash !== parent.hash) return [false, "parent_not_cited"];
    if (!AUTHORISING.includes(cited.state)) return [false, "parent_not_warranted"];
  }
  if (item.type !== obligation.requires || (obligation.name && item.name !== obligation.name)) return [false, "wrong_type"];
  return [true, null];
}

/** Rule 7: a named reviewer, linked by digest to exactly the material they were shown. */
export function humanLinked(human, digests) {
  human = human || {};
  if (!human.reviewer) return [false, "unnamed_reviewer"];
  const shown = human.shown || [];
  const known = new Set(digests);
  if (!shown.length || shown.some((d) => !known.has(d))) return [false, "material_not_linked"];
  return [true, null];
}

/** Every digest a reviewer could have been shown on this record. */
export function recordDigests(record) {
  const out = (record.evidence || []).filter((e) => e.content_hash).map((e) => e.content_hash);
  const stateDigest = record.decision?.state_digest;
  if (stateDigest) out.push(stateDigest);
  return out;
}

/**
 * Evaluate a decision record's obligations and derive the state its mandate and evidence allow.
 * `at` is the verdict time for freshness; by default the record's `verdict.at` or timestamp. Items
 * are judged only against the obligation they were offered for (`evidence[].obligation`).
 */
export function assess(record, { at } = {}) {
  const actorName = record.actor?.name;
  const when = parseTimestamp(at || record.verdict?.at || record.timestamp);
  const parents = new Map();
  for (const p of record.parents || []) if (p && typeof p === "object" && "record_id" in p) parents.set(p.record_id, p);
  const evidence = record.evidence || [];
  const digests = recordDigests(record);

  const admissions = {};
  const obligations = [];
  const met = [];
  const unmet = [];
  const advisoryUnmet = [];
  for (const ob of record.obligations || []) {
    const satisfied = [];
    if (ob.requires === "human_review") {
      const [ok] = humanLinked(record.human, digests);
      if (ok) satisfied.push(`human:${pyStr((record.human || {}).reviewer)}`);
    }
    evidence.forEach((item, index) => {
      if (item.obligation !== ob.id) return;
      let [admitted, reason] = admit(item, ob, { actorName, at: when, parents });
      if (admitted && ob.requires === "human_review") [admitted, reason] = humanLinked(record.human, digests);
      admissions[index] = admitted ? { status: "admitted" } : { status: "rejected", reason: reason || "" };
      if (admitted) satisfied.push(item.name ?? `evidence[${index}]`);
    });
    obligations.push({ ...ob, met: satisfied.length > 0, satisfied_by: satisfied });
    if (satisfied.length) met.push(ob.id);
    else if (ob.kind === "advisory") advisoryUnmet.push(ob.id);
    else unmet.push(ob.id);
  }
  const [state, history] = deriveState(record, unmet);
  return { obligations, admissions, met, unmet, advisoryUnmet, state, history };
}

/**
 * The state a decision reached within its own scope, and the path it took there. A record with
 * neither obligations nor a verdict is an ordinary (L1) record and has no state.
 */
export function deriveState(record, unmet) {
  if (!(record.obligations && record.obligations.length) && !record.verdict) return [null, []];
  const result = record.mandate?.result ?? "unchecked";
  const status = record.decision?.status;
  if (result === "deny") return ["refused", ["proposed", "refused"]];
  if (result === "escalate") return ["escalated", ["proposed", "escalated"]];
  if (unmet.length) return ["pending_evidence", ["proposed", "pending_evidence"]];
  if (status === "acted") return ["committed", ["proposed", "warranted", "committed"]];
  return ["warranted", ["proposed", "warranted"]];
}

/** A reason the path is illegal, or `null`. */
export function checkHistory(history) {
  if (!history || !history.length) return null;
  if (history[0] !== "proposed") return `history starts at ${pyRepr(history[0])}, not 'proposed'`;
  for (let i = 1; i < history.length; i++) {
    if (!legal(history[i - 1], history[i])) return `illegal transition ${pyStr(history[i - 1])} -> ${pyStr(history[i])}`;
  }
  return null;
}

/** A reason a later transition record is not allowed from `current`, or `null`. */
export function checkTransition(decision, current, transition) {
  const verdict = transition.verdict || {};
  const toState = verdict.state;
  const fromState = verdict.from_state;
  if (fromState !== undefined && fromState !== null && fromState !== current) {
    return `transition claims to leave ${pyRepr(fromState)} but the decision is ${pyRepr(current)}`;
  }
  if (!legal(current, toState)) return `illegal transition ${pyStr(current)} -> ${pyStr(toState)}`;
  if (toState === "warranted" && current === "escalated") {
    const [ok, reason] = humanLinked(transition.human, recordDigests(decision));
    if (!ok) return `a human decision to warrant needs a named reviewer linked to what they were shown (${reason})`;
  }
  if (toState === "warranted" && current === "pending_evidence") {
    // Evidence cannot be added to a sealed record, so a later transition can supply only a person.
    const unmetIds = new Set(decision.verdict?.unmet || []);
    const kinds = new Map((decision.obligations || []).map((o) => [o.id, o.requires]));
    const other = [...unmetIds].filter((ob) => kinds.get(ob) !== "human_review").sort();
    if (other.length) return `unmet obligations ${other.join(", ")} need evidence, which a transition cannot add; record a new decision that cites this one`;
    const [ok, reason] = humanLinked(transition.human, recordDigests(decision));
    if (!ok) return `a human sign-off needs a named reviewer linked to what they were shown (${reason})`;
  }
  if (!verdict.decided_by) return "a transition must say who decided it";
  return null;
}
