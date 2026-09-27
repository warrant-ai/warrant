// Compiled with `npm run typecheck`, never run: proves the declarations match how the SDK is used.
import { Decision, HttpSink, Keyring, Redactor, SigningKey, Verdict, Warrant, currentDecision, merkleRoot, saltedHash, verifySeal, type PolicyEngine, type Sink } from "warrantai";

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

const key = SigningKey.fromPrivateBytes("demo-bank", new Uint8Array(32));
const ring = new Keyring([key.public]);
const check = verifySeal({}, ring);
const issuer: string | undefined = check.ok ? check.issuer : undefined;
const root: string = merkleRoot(["00".repeat(32)]);
const salted: string = saltedHash("x", new Uint8Array(32));
new Warrant("lending", { store: sink, agent: { name: "a", version: "1", identity: ["npci-agent-registry", "AGT-1"] } });
w.decide("credit.approve", { subject: "LN-2" }, (d) => {
  const s: string = d.evidence("pan", { uri: "kyc://1", content: "x", sensitive: true, provider: "nsdl", obligation: "OB-1" });
  d.claim("turnover", 1);
  d.cite({}, { keyring: ring, state: "committed" });
  d.retention("rbi-credit-8y", { legalHold: true });
  return s;
});

// @ts-expect-error not a lifecycle state
w.decide("credit.approve", { subject: "x" }, (d) => d.cite({}, { state: "approved" }));
// @ts-expect-error cite needs the parent record
w.decide("credit.approve", { subject: "x" }, (d) => d.cite());
// @ts-expect-error a keyring, not a key set document
w.decide("credit.approve", { subject: "x" }, (d) => d.cite({}, { keyring: { keys: [] } }));

void [approved, flushed, open, issuer, root, salted];
