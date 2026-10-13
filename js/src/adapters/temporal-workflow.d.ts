// Workflow-side helpers, imported in workflow code as "warrantai/adapters/temporal/workflow".

export const DENIED: "WarrantDenied";
export const ESCALATED: "WarrantEscalated";
export const EVIDENCE_HEADER: "warrant-evidence";
export const APPROVAL_HEADER: "warrant-approval";
export const RECORD_ACTIVITY: "warrant.record";

/** The details a `WarrantDenied` or `WarrantEscalated` failure carries. */
export interface BlockedDetails {
  record_id: string;
  result: "deny" | "escalate";
  decision_class: string;
  subject: string;
  policy_id: string | null;
  clause: string | null;
  reason: string | null;
  [key: string]: unknown;
}

/** The mandate's verdict on a decision recorded by `decide()`. */
export interface WorkflowVerdict {
  record_id: string;
  status: "acted" | "withheld";
  result: "allow" | "deny" | "escalate" | "unchecked";
  policy_id: string | null;
  policy_version: string | null;
  clause: string | null;
  reason: string | null;
  flagged: boolean;
}

export interface DecideOptions {
  subject: string;
  inputs: Record<string, unknown>;
  action: string;
  summary?: string;
  alternatives?: string[];
  onBehalfOf?: string;
}

/** The details of a blocked decision, or `null` for any other error. */
export function blocked(err: unknown): BlockedDetails | null;
/** The details of an escalated decision, or `null`. A denial is not an escalation. */
export function escalation(err: unknown): BlockedDetails | null;
/** Record a decision the workflow itself makes and return the verdict to branch on. */
export function decide(decisionClass: string, options: DecideOptions): Promise<WorkflowVerdict>;
/** Run an escalated activity now that a person has approved it. `options` also takes the activity options (timeouts, retry, task queue). */
export function approved<T = unknown>(activityType: string, args: unknown[], options: { reviewer: string; recordId: string; note?: string; [option: string]: unknown }): Promise<T>;
/** Record a person's verdict on a decision as a linked record. */
export function verdict(recordId: string, options: { reviewer: string; verdict: "approve" | "reject" | "amend"; note?: string }): Promise<{ record_id: string; verdict: string }>;
/** Record that a person rejected an escalated decision, or that the wait for one timed out. */
export function rejected(recordId: string, options: { reviewer: string; note?: string }): Promise<{ record_id: string; verdict: string }>;
/** The interceptors factory a workflow interceptor module exports. */
export function warrantWorkflowInterceptors(decisionTypes: string[]): () => { outbound: unknown[] };
