// Names and helpers shared by the activity side (temporal.js) and the workflow side
// (temporal-workflow.js). No imports beyond pure modules, so workflow code can bundle it.

import { canonicalJson } from "../canonical.js";
import { sha256Hex, utf8 } from "../sha256.js";

export const DENIED = "WarrantDenied";
export const ESCALATED = "WarrantEscalated";
export const UNREADABLE = "WarrantUnreadable";
/** Header the workflow interceptor puts the run's evidence in; the Python adapter uses the same name and item shape. */
export const EVIDENCE_HEADER = "warrant-evidence";
/** Header `approved()` puts a reviewer's approval in: `{ record_id, reviewer, note }`, as the Python adapter writes and reads it. */
export const APPROVAL_HEADER = "warrant-approval";
/** The workflow-side record activity, in both SDKs; its results are Warrant's own writes, not evidence. */
export const RECORD_ACTIVITY = "warrant.record";
export const MAX_EVIDENCE = 100;

/** Something a content hash can cover: JSON as is, anything else by its text. */
export function asEvidenceContent(value) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return String(value);
  }
}

/** `contentHash(asEvidenceContent(value))` from hashing.js, computed without node:crypto. */
export function evidenceHash(value) {
  const content = asEvidenceContent(value);
  return sha256Hex(utf8(typeof content === "string" ? content : canonicalJson(content)));
}
