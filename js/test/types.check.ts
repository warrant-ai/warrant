// Compiled with `npm run typecheck`, never run: proves the declarations match how the SDK is used.
import { Decision, HttpSink, Redactor, Verdict, Warrant, currentDecision, type PolicyEngine, type Sink } from "warrantai";

const policy: PolicyEngine = { evaluate: (_cls, inputs) => new Verdict(Number(inputs.amount) > 5 ? "deny" : "allow", { policyId: "CR-07" }) };
const sink: Sink = { write: async (records) => void records.length };
const w = new Warrant("lending", { store: sink, policy, redact: new Redactor({ patterns: [/\d{10}/] }), agent: { name: "a", version: "1" } });

const approved: Promise<boolean> = w.decide("credit.approve", { subject: "LN-1" }, async (d: Decision) => {
  const verdict = d.check({ amount: 1 });
  const hash: string = d.evidence("bureau", { uri: "cibil://1", content: { score: 748 }, type: "tool_call" });
  d.modelCall("anthropic", "claude-sonnet-5", { tokensIn: 1, tokensOut: 1, amount: 0.1 });
  if (verdict.allowed) d.act("approve", { summary: hash, costCentre: "retail" });
  return d.acted;
});
const id: string = w.outcome({ label: "performing", decisionRecordId: "01K5TEST000000000000000001" });
w.humanVerdict({ reviewer: "asha", verdict: "approve", decisionRecordId: id });
const flushed: Promise<boolean> = w.flush(1000);
const open: Decision | undefined = currentDecision();
new HttpSink("https://collector.internal", "token", { timeoutMs: 1 });

// @ts-expect-error subject is required
w.decide("credit.approve", {}, () => {});
// @ts-expect-error not a mandate result
new Verdict("maybe");
// @ts-expect-error not a verdict
w.humanVerdict({ reviewer: "asha", verdict: "maybe", decisionRecordId: id });
// @ts-expect-error evidence needs a uri
w.decide("credit.approve", { subject: "x" }, (d) => d.evidence("e", { content: "c" }));

void [approved, flushed, open];
