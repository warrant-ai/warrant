/**
 * Policy bundles: versioned CEL policies mapped to decision classes, evaluated in-process.
 * The same files and the same semantics as the Python SDK's `warrant.policy`:
 *
 *   policy_id: CR-07
 *   version: "2026.3"
 *   classes: [credit.approve]
 *   fail_mode: closed          # what check() returns if a clause cannot be evaluated
 *   default: deny              # result when no clause matches
 *   clauses:
 *     - id: "4.2"
 *       title: Auto-approve within limit
 *       when: amount <= 500000 && bureau_score >= 720 && double(foir) <= 0.45
 *       result: allow
 *
 * Clauses are evaluated in order; the first whose `when` is true decides. Needs the
 * optional packages `@marcbachmann/cel-js` (Node.js 20.19 or newer) and, for YAML files, `yaml`.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { basename, extname, join } from "node:path";
import { EVIDENCE_TYPES, MANDATE_RESULTS, Verdict } from "./client.js";

export const FAIL_MODES = ["closed", "open", "escalate"];
export const CLAUSE_RESULTS = ["allow", "deny", "escalate"];
const CLASS_PATTERN = /^[a-z0-9_]+(\.[a-z0-9_]+)*(\.\*)?$/;
const FAIL_RESULT = { closed: "deny", open: "allow", escalate: "escalate" };
const POLICY_FILE = /\.(ya?ml|json)$/i;
const OBLIGATION_KINDS = ["verifiable", "advisory"];
const DURATION = /^\s*(\d+)\s*([smhd]?)\s*$/;
const UNIT_SECONDS = { "": 1, s: 1, m: 60, h: 3600, d: 86400 };
// An input compared directly with a decimal literal. A whole-number input (0, 1, 40) is an
// int, and CEL engines disagree on int-versus-double comparison; double(x) is portable.
const BARE_DECIMAL_COMPARISON = /(?<![\w.)])([a-z_][\w.]*)\s*(<=|>=|==|!=|<|>)\s*\d+\.\d+|\d+\.\d+\s*(<=|>=|==|!=|<|>)\s*([a-z_][\w.]*)(?![\w.(])/i;

/** A policy file is malformed, a CEL expression does not parse, or the bundle is inconsistent. */
export class PolicyError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "PolicyError";
  }
}

async function optional(name, why) {
  try {
    return await import(name);
  } catch (err) {
    if (err?.code === "ERR_MODULE_NOT_FOUND") throw new PolicyError(`${why} needs the ${name} package: npm install ${name}`, { cause: err });
    throw err;
  }
}

// One input divided by another. Whole numbers divide as integers (30000 / 50000 is 0).
const BARE_DIVISION = /(?<![\w.)])([a-z_][\w.]*)\s*\/\s*([a-z_][\w.]*)(?![\w.(])/i;

/** Warn-worthy patterns in a clause. Returned, not thrown: the policy still loads. */
export function lintClause(when) {
  const warnings = [];
  for (const match of when.matchAll(new RegExp(BARE_DECIMAL_COMPARISON, "gi"))) {
    // `a / b <= 0.45` compares the quotient, not b; the division warning covers it.
    if (/[-+*\/%]\s*$/.test(when.slice(0, match.index))) continue;
    const name = match[1] ?? match[4];
    warnings.push(`compares ${name} with a decimal literal; a whole-number input cannot be evaluated by every engine, write double(${name})`);
  }
  const division = BARE_DIVISION.exec(when);
  if (division) {
    warnings.push(`divides ${division[1]} by ${division[2]}; whole numbers divide as integers, write double(${division[1]}) / double(${division[2]})`);
  }
  return warnings;
}

/** `30d`, `12h`, `90m`, `45s` or a whole number of seconds, as in the Python SDK. */
export function parseDuration(value, where) {
  if (typeof value === "boolean") throw new PolicyError(`${where}: not a duration: ${JSON.stringify(value)}`);
  if (typeof value === "number" && Number.isInteger(value)) {
    if (value < 0) throw new PolicyError(`${where}: a duration cannot be negative`);
    return value;
  }
  const match = DURATION.exec(String(value));
  if (!match) throw new PolicyError(`${where}: not a duration (use e.g. 30d, 12h, 90m or seconds): ${JSON.stringify(value)}`);
  return Number(match[1]) * UNIT_SECONDS[match[2]];
}

function parseObligations(name, raw, env, clauseIds) {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) throw new PolicyError(`${name}: 'obligations' must be a list`);
  const seen = new Set();
  return raw.map((ro, i) => {
    let where = `${name}: obligation #${i + 1}`;
    if (ro === null || typeof ro !== "object" || Array.isArray(ro)) throw new PolicyError(`${where} must be a mapping`);
    const id = String(ro.id ?? "").trim();
    if (!id) throw new PolicyError(`${where} has no id`);
    if (seen.has(id)) throw new PolicyError(`${name}: duplicate obligation id ${JSON.stringify(id)}`);
    seen.add(id);
    where = `${name}: obligation ${id}`;
    const requires = String(ro.requires ?? "");
    if (!EVIDENCE_TYPES.includes(requires)) throw new PolicyError(`${where}: requires must be one of ${EVIDENCE_TYPES.join(", ")}, got ${JSON.stringify(requires)}`);
    const kind = String(ro.kind ?? "verifiable");
    if (!OBLIGATION_KINDS.includes(kind)) throw new PolicyError(`${where}: kind must be verifiable or advisory, got ${JSON.stringify(kind)}`);
    const providers = ro.providers ?? [];
    if (!Array.isArray(providers) || !providers.every((p) => typeof p === "string" && p)) throw new PolicyError(`${where}: providers must be a list of provider names`);
    if (providers.includes("self")) throw new PolicyError(`${where}: 'self' cannot be a qualified provider; the acting agent never supplies evidence for its own obligation`);
    const maxAge = ro.max_age !== undefined && ro.max_age !== null ? parseDuration(ro.max_age, `${where}: max_age`) : undefined;
    const clause = ro.clause === undefined || ro.clause === null ? undefined : String(ro.clause);
    if (clause !== undefined && !clauseIds.has(clause)) throw new PolicyError(`${where}: clause ${JSON.stringify(ro.clause)} is not a clause of this policy`);
    let program;
    if (ro.when !== undefined && ro.when !== null) {
      if (typeof ro.when !== "string" || !ro.when.trim()) throw new PolicyError(`${where}: 'when' must be a CEL expression string`);
      try {
        program = env.parse(ro.when);
      } catch (err) {
        throw new PolicyError(`${where}: CEL parse error: ${String(err.message).split("\n")[0]}`, { cause: err });
      }
    }
    return {
      id, requires, kind, providers: [...providers], maxAgeSeconds: maxAge,
      name: ro.name ? String(ro.name) : undefined, clause, title: ro.title ? String(ro.title) : undefined,
      when: ro.when ? String(ro.when).trim() : undefined, program,
    };
  });
}

function parseRetention(name, raw) {
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== "object" || Array.isArray(raw) || typeof raw.class !== "string" || !raw.class) {
    throw new PolicyError(`${name}: retention must be a mapping with a 'class' name`);
  }
  const out = { class: raw.class };
  if (raw.period !== undefined && raw.period !== null) out.seconds = parseDuration(raw.period, `${name}: retention period`);
  return out;
}

/** The obligation as it is written onto a record. */
function obligationRecord(spec, policy) {
  const out = { id: spec.id, requires: spec.requires, kind: spec.kind, policy_id: policy.policyId, policy_version: policy.version };
  if (spec.providers.length) out.providers = [...spec.providers];
  if (spec.maxAgeSeconds !== undefined) out.max_age_seconds = spec.maxAgeSeconds;
  for (const key of ["name", "clause", "title"]) if (spec[key] !== undefined) out[key] = spec[key];
  return out;
}

/** JSON numbers to CEL numbers, as the Python SDK maps them: whole numbers are ints, the rest doubles. */
function toCel(value) {
  if (Array.isArray(value)) return value.map(toCel);
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, toCel(v)]));
  if (typeof value === "number" && Number.isSafeInteger(value)) return BigInt(value);
  return value;
}

function parsePolicy(name, raw, cel, logger) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) throw new PolicyError(`${name}: top level must be a mapping`);
  const needText = (key) => {
    const value = raw[key];
    if (typeof value !== "string" || !value.trim()) throw new PolicyError(`${name}: '${key}' must be a non-empty string`);
    return value.trim();
  };
  const oneOf = (key, fallback, allowed) => {
    const value = String(raw[key] ?? fallback);
    if (!allowed.includes(value)) throw new PolicyError(`${name}: ${key} must be one of ${allowed.join(", ")}, got ${JSON.stringify(value)}`);
    return value;
  };

  const policyId = needText("policy_id");
  const version = String(raw.version ?? "").trim();
  if (!version) throw new PolicyError(`${name}: 'version' must be a non-empty string`);
  const classes = raw.classes;
  if (!Array.isArray(classes) || !classes.length || !classes.every((c) => typeof c === "string")) {
    throw new PolicyError(`${name}: 'classes' must be a non-empty list of decision classes`);
  }
  for (const c of classes) {
    if (!CLASS_PATTERN.test(c)) throw new PolicyError(`${name}: invalid class pattern ${JSON.stringify(c)} (expected e.g. credit.approve or credit.*)`);
  }
  const failMode = oneOf("fail_mode", "closed", FAIL_MODES);
  const fallback = oneOf("default", "deny", CLAUSE_RESULTS);

  if (!Array.isArray(raw.clauses) || !raw.clauses.length) throw new PolicyError(`${name}: 'clauses' must be a non-empty list`);
  const env = new cel.Environment({ unlistedVariablesAreDyn: true });
  const seen = new Set();
  const clauses = raw.clauses.map((rc, i) => {
    if (rc === null || typeof rc !== "object" || Array.isArray(rc)) throw new PolicyError(`${name}: clause #${i + 1} must be a mapping`);
    const id = String(rc.id ?? "").trim();
    if (!id) throw new PolicyError(`${name}: clause #${i + 1} has no id`);
    if (seen.has(id)) throw new PolicyError(`${name}: duplicate clause id ${JSON.stringify(id)}`);
    seen.add(id);
    if (typeof rc.when !== "string" || !rc.when.trim()) throw new PolicyError(`${name}: clause ${id}: 'when' must be a CEL expression string`);
    const result = String(rc.result ?? "");
    if (!CLAUSE_RESULTS.includes(result)) throw new PolicyError(`${name}: clause ${id}: result must be one of ${CLAUSE_RESULTS.join(", ")}, got ${JSON.stringify(result)}`);
    let program;
    try {
      program = env.parse(rc.when);
    } catch (err) {
      throw new PolicyError(`${name}: clause ${id}: CEL parse error: ${String(err.message).split("\n")[0]}`, { cause: err });
    }
    for (const warning of lintClause(rc.when)) logger.warn(`warrant policy ${policyId} clause ${id} ${warning}`);
    return { id, when: rc.when.trim(), result, title: rc.title ? String(rc.title) : undefined, program };
  });

  const tests = (raw.tests ?? []).map((rt, i) => {
    if (rt === null || typeof rt !== "object" || rt.inputs === null || typeof rt.inputs !== "object" || Array.isArray(rt.inputs)) {
      throw new PolicyError(`${name}: test #${i + 1} must be a mapping with an 'inputs' mapping`);
    }
    const expect = String(rt.expect ?? "");
    if (!MANDATE_RESULTS.includes(expect)) throw new PolicyError(`${name}: test #${i + 1}: expect must be one of ${MANDATE_RESULTS.join(", ")}, got ${JSON.stringify(expect)}`);
    return { name: String(rt.name || `test #${i + 1}`), inputs: { ...rt.inputs }, expect, clause: rt.clause === undefined || rt.clause === null ? undefined : String(rt.clause) };
  });

  const obligations = parseObligations(name, raw.obligations, env, new Set(clauses.map((c) => c.id)));
  if (raw.enforce !== undefined && typeof raw.enforce !== "boolean") throw new PolicyError(`${name}: 'enforce' must be true or false`);
  const retention = parseRetention(name, raw.retention);

  return {
    policyId, version, classes: classes.map((c) => c.trim()), clauses, failMode, default: fallback,
    title: raw.title ? String(raw.title) : undefined, tests, source: name,
    obligations, enforce: raw.enforce === true, retention,
  };
}

/** A set of policies with a lookup from decision class to the governing policy. */
export class PolicyBundle {
  constructor(policies) {
    this.policies = [...policies];
    this._exact = new Map();
    this._prefix = new Map();
    for (const policy of this.policies) {
      for (const cls of policy.classes) {
        const wildcard = cls.endsWith(".*");
        const table = wildcard ? this._prefix : this._exact;
        const key = wildcard ? cls.slice(0, -2) : cls;
        if (table.has(key)) throw new PolicyError(`class ${JSON.stringify(cls)} is claimed by both ${table.get(key).policyId} and ${policy.policyId}`);
        table.set(key, policy);
      }
    }
  }

  /** Load every `*.yaml`, `*.yml` and `*.json` file in a directory, or one file. */
  static async load(path, { logger = console } = {}) {
    let stat;
    try {
      stat = statSync(path);
    } catch (err) {
      if (err.code === "ENOENT") throw new PolicyError(`policy bundle not found: ${path}`, { cause: err });
      throw err;
    }
    const files = stat.isFile() ? [path] : readdirSync(path).filter((n) => POLICY_FILE.test(n)).sort().map((n) => join(path, n));
    if (!files.length) throw new PolicyError(`no policy files (*.yaml, *.yml, *.json) in ${path}`);
    const cel = await optional("@marcbachmann/cel-js", "policy bundles");
    const yaml = files.some((f) => extname(f).toLowerCase() !== ".json") ? await optional("yaml", "YAML policy files") : null;
    const policies = files.map((file) => {
      const name = basename(file);
      let raw;
      try {
        const text = readFileSync(file, "utf8");
        raw = extname(file).toLowerCase() === ".json" ? JSON.parse(text) : yaml.parse(text);
      } catch (err) {
        throw new PolicyError(`${name}: cannot parse: ${String(err.message).split("\n")[0]}`, { cause: err });
      }
      return parsePolicy(name, raw, cel, logger);
    });
    const bundle = new PolicyBundle(policies);
    logger.info(`warrant policy bundle loaded: ${policies.length} policy file(s) from ${path}`);
    return bundle;
  }

  /** Exact class match first, then the longest matching `prefix.*` pattern. */
  policyFor(decisionClass) {
    if (this._exact.has(decisionClass)) return this._exact.get(decisionClass);
    const parts = decisionClass.split(".");
    for (let i = parts.length - 1; i > 0; i--) {
      const prefix = parts.slice(0, i).join(".");
      if (this._prefix.has(prefix)) return this._prefix.get(prefix);
    }
    return undefined;
  }
}

/** `PolicyEngine` over a loaded bundle. Evaluation is synchronous and in-process. */
export class CelPolicyEngine {
  constructor(bundle, { logger = console } = {}) {
    if (!(bundle instanceof PolicyBundle)) throw new TypeError("CelPolicyEngine needs a PolicyBundle: await PolicyBundle.load(path)");
    this.bundle = bundle;
    this._log = logger;
  }

  evaluate(decisionClass, inputs) {
    const policy = this.bundle.policyFor(decisionClass);
    if (!policy) return new Verdict("unchecked", { reason: `no policy governs class ${decisionClass}` });
    let activation;
    try {
      if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs)) throw new TypeError("inputs must be a mapping");
      activation = toCel(JSON.parse(JSON.stringify(inputs)));
    } catch (err) {
      return this._fail(policy, `inputs are not JSON-serialisable: ${err.message}`);
    }
    for (const clause of policy.clauses) {
      let matched;
      try {
        matched = clause.program(activation);
      } catch (err) {
        return this._fail(policy, `clause ${clause.id}: ${String(err.message).split("\n")[0].slice(0, 160)}`);
      }
      if (typeof matched !== "boolean") return this._fail(policy, `clause ${clause.id}: expression returned ${typeof matched}, not bool`);
      if (matched) {
        return this._withAdr(policy, activation, { result: clause.result, policyId: policy.policyId, policyVersion: policy.version, clause: clause.id, reason: clause.title ?? `clause ${clause.id} matched` });
      }
    }
    return this._withAdr(policy, activation, { result: policy.default, policyId: policy.policyId, policyVersion: policy.version, reason: "no clause matched" });
  }

  /**
   * Attach the obligations that apply, the enforcement mode and the retention class. An obligation
   * whose `when` cannot be evaluated applies: more evidence is the only safe direction.
   */
  _withAdr(policy, activation, fields) {
    const { result, ...rest } = fields;
    if (!policy.obligations.length && !policy.enforce && policy.retention === undefined) return new Verdict(result, rest);
    const obligations = [];
    for (const spec of policy.obligations) {
      if (spec.program !== undefined && activation !== null) {
        let applies;
        try {
          applies = spec.program(activation);
        } catch (err) {
          this._log.warn(`warrant policy ${policy.policyId} obligation ${spec.id} condition could not evaluate (${String(err.message).split("\n")[0].slice(0, 160)}); it applies`);
          applies = true;
        }
        if (!applies) continue;
      }
      obligations.push(obligationRecord(spec, policy));
    }
    return new Verdict(result, { ...rest, obligations, enforce: policy.enforce, retention: policy.retention });
  }

  _fail(policy, detail) {
    this._log.warn(`warrant policy ${policy.policyId} could not evaluate (${detail}); fail-${policy.failMode} applied`);
    return this._withAdr(policy, null, { result: FAIL_RESULT[policy.failMode], policyId: policy.policyId, policyVersion: policy.version, reason: `fail-${policy.failMode}: ${detail}`, flagged: true });
  }
}

/** Run every policy's embedded tests. Each test evaluates against the policy's first class. */
export function runPolicyTests(bundle, { logger = { info() {}, warn() {}, error() {} } } = {}) {
  const engine = new CelPolicyEngine(bundle, { logger });
  const results = [];
  for (const policy of bundle.policies) {
    const first = policy.classes[0];
    const target = first.endsWith(".*") ? `${first.slice(0, -2)}.test` : first;
    for (const test of policy.tests) {
      const verdict = engine.evaluate(target, test.inputs);
      const passed = verdict.result === test.expect && (test.clause === undefined || verdict.clause === test.clause);
      let detail = "";
      if (!passed) {
        const want = test.expect + (test.clause ? ` via clause ${test.clause}` : "");
        const got = verdict.result + (verdict.clause ? ` via clause ${verdict.clause}` : "");
        detail = `expected ${want}, got ${got} (${verdict.reason})`;
      }
      results.push({ policyId: policy.policyId, name: test.name, passed, expected: test.expect, got: verdict.result, detail });
    }
  }
  return results;
}
