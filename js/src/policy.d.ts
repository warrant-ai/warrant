/** Policy bundles for the Warrant JavaScript SDK. Needs `@marcbachmann/cel-js` and, for YAML files, `yaml`. */

import type { Logger, MandateResult, PolicyEngine, Verdict } from "./index.js";

export const FAIL_MODES: readonly ["closed", "open", "escalate"];
export const CLAUSE_RESULTS: readonly ["allow", "deny", "escalate"];
export type FailMode = (typeof FAIL_MODES)[number];
export type ClauseResult = (typeof CLAUSE_RESULTS)[number];

export class PolicyError extends Error {}

export interface Clause {
  id: string;
  when: string;
  result: ClauseResult;
  title?: string;
}

export interface PolicyTest {
  name: string;
  inputs: Record<string, unknown>;
  expect: MandateResult;
  clause?: string;
}

export interface Policy {
  policyId: string;
  version: string;
  classes: string[];
  clauses: Clause[];
  failMode: FailMode;
  default: ClauseResult;
  title?: string;
  tests: PolicyTest[];
  source: string;
}

export interface PolicyTestResult {
  policyId: string;
  name: string;
  passed: boolean;
  expected: MandateResult;
  got: MandateResult;
  detail: string;
}

export class PolicyBundle {
  /** Load every `*.yaml`, `*.yml` and `*.json` file in a directory, or one file. */
  static load(path: string, options?: { logger?: Logger }): Promise<PolicyBundle>;
  readonly policies: Policy[];
  /** Exact class match first, then the longest matching `prefix.*` pattern. */
  policyFor(decisionClass: string): Policy | undefined;
}

export class CelPolicyEngine implements PolicyEngine {
  constructor(bundle: PolicyBundle, options?: { logger?: Logger });
  readonly bundle: PolicyBundle;
  evaluate(decisionClass: string, inputs: Readonly<Record<string, unknown>>): Verdict;
}

export function runPolicyTests(bundle: PolicyBundle, options?: { logger?: Logger }): PolicyTestResult[];
/** Patterns worth a warning, such as comparing an input with a decimal literal without double(). */
export function lintClause(when: string): string[];
