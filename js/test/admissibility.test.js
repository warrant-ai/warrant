// conformance/admissibility-cases.json is generated from the Python SDK. The JS port must
// reproduce every case exactly: reason codes, met and unmet in order, state, history, messages.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { EDGES, STATES, assess, checkHistory, checkTransition, legal } from "../src/admissibility.js";

const cases = JSON.parse(readFileSync(new URL("../../conformance/admissibility-cases.json", import.meta.url), "utf8"));

test("states, edges and every legal pair match Python", () => {
  assert.deepEqual(STATES, cases.states);
  assert.deepEqual({ ...EDGES }, cases.edges);
  for (const [a, b, expected] of cases.legal) assert.equal(legal(a, b), expected, `${a} -> ${b}`);
});

test("every assess case matches Python", () => {
  for (const c of cases.assess) {
    const a = assess(c.record, { at: c.at ?? undefined });
    const got = {
      obligations: a.obligations,
      admissions: Object.fromEntries(Object.entries(a.admissions)),
      met: a.met,
      unmet: a.unmet,
      advisory_unmet: a.advisoryUnmet,
      state: a.state,
      history: a.history,
    };
    assert.deepEqual(got, c.expected, c.name);
  }
});

test("every history case matches Python", () => {
  for (const c of cases.history) assert.equal(checkHistory(c.history), c.expected, c.name);
});

test("every transition case matches Python, messages included", () => {
  for (const c of cases.transition) assert.equal(checkTransition(c.decision, c.current, c.transition), c.expected, c.name);
});
