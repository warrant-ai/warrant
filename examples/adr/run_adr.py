"""The thesis's worked flow, running: one loan, two organisations, three agents, one witness.

A partner's data agent verifies GST filings and issues a signed record. The bank's credit agent
cites that record instead of trusting the partner's message, pulls the bureau itself, and may only
commit on a warrant. An auditor-run witness co-signs the bank's checkpoints without holding a single
record. Then a verifier with nothing but the two exports and the public keys reaches level 3,
traces the loan back to the partner's step, and watches the witness refuse a rewritten history.

Illustrative data, generated as it runs. Run: python examples/adr/run_adr.py [workdir]
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from warrant import AgentInfo, NotWarranted, Warrant
from warrant import checkpoint as cp
from warrant.signing import Keyring, SigningKey
from warrant.store import SQLiteStore
from warrant.trace import trace
from warrant.verify import verify_records

HERE = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def export(db: Path, stream: str) -> list:
    store = SQLiteStore(db, read_only=True)
    try:
        return list(store.iter_records(stream))
    finally:
        store.close()


def write_jsonl(records: list, path: Path) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def main(workdir: Path) -> int:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    keys = workdir / "keys"

    # Each organisation holds its own key; only the public key sets are shared.
    partner_key = SigningKey.generate("partner-data")
    bank_key = SigningKey.generate("demo-bank")
    witness_key = SigningKey.generate("audit-witness")
    for key in (partner_key, bank_key, witness_key):
        key.save(keys)
    public = Keyring.load(*(keys / f"{k.issuer}.keys.json" for k in (partner_key, bank_key, witness_key)))
    partner_public = Keyring.load(keys / "partner-data.keys.json")

    # 1. The partner's data agent verifies GST filings with the GST network as the provider.
    partner = Warrant("gst", tenant="partner-data", store=workdir / "partner.db", signing_key=partner_key,
                      agent=AgentInfo("gst-agent", "1.2.0", runtime="temporal"), policy_bundle=HERE / "partner-policies")
    gstn_response = {"gstin": "29ABCDE1234F1Z5", "returns_filed": 12, "turnover_band": "4-5 crore"}
    with partner.decide("gst.verify", subject="GSTIN:29ABCDE1234F1Z5") as d:
        d.check(filings_found=True)
        gstn_digest = d.evidence("gstn_returns", uri="gstn://returns/29ABCDE1234F1Z5/FY26", type="tool_call",
                                 provider="gstn", content=gstn_response, retrieved_at=now(), sensitive=True)
        d.claim("gst_turnover_inr", 42_000_000)
        d.commit("verified")
    partner.flush()
    partner_records = export(workdir / "partner.db", "gst")
    gst_record = partner_records[-1]
    write_jsonl(partner_records, workdir / "partner-export.jsonl")
    print(f"partner: GST verification {gst_record['record_id']} is {gst_record['verdict']['state']}, "
          f"signed {gst_record['seal']['key_id']}; the GSTN digest is salted")

    # 2. The bank's credit agent relies on the partner's record, never on its claim.
    bank = Warrant("lending", tenant="demo-bank", store=workdir / "bank.db", signing_key=bank_key,
                   agent=AgentInfo("credit-agent", "3.0.1", model="laya@55cf4c4ebb4e/multilingual"),
                   policy_bundle=HERE / "policies")
    with bank.decide("credit.msme.approve", subject="LN-7731") as d:
        d.check(amount=2_000_000, bureau_score=742)
        d.evidence("bureau_pull", uri="cibil://req/55120", type="tool_call", provider="cibil",
                   content={"score": 742}, retrieved_at=now())
        d.cite(gst_record, keyring=partner_public)
        state = d.warrant()
        d.commit("approve", route="auto")
    loan = d.record_id
    print(f"bank: LN-7731 {state.state} (met {', '.join(state.met)}) and committed")

    # 3. The same agent tries to use the partner's claim as its own evidence. Fail closed.
    with bank.decide("credit.msme.approve", subject="LN-7732") as d:
        d.check(amount=1_500_000, bureau_score=731)
        d.evidence("bureau_pull", uri="cibil://req/55121", type="tool_call", provider="cibil",
                   content={"score": 731}, retrieved_at=now())
        d.evidence("partner says verified", uri="msg://partner/881", type="record", provider="self",
                   content={"turnover": 42_000_000}, obligation="OB-2")
        try:
            d.commit("approve")
        except NotWarranted as exc:
            print(f"bank: LN-7732 refused to act: {exc}")

    # 4. A larger ticket needs a named officer who saw exactly what the agent saw.
    with bank.decide("credit.msme.approve", subject="LN-7733") as d:
        d.check(amount=3_500_000, bureau_score=765)
        bureau = d.evidence("bureau_pull", uri="cibil://req/55122", type="tool_call", provider="cibil",
                            content={"score": 765}, retrieved_at=now())
        d.cite(gst_record, keyring=partner_public)
        print(f"bank: LN-7733 is {d.warrant().state} until a credit officer signs off")
    big = d.record_id
    bank.transition(big, "warranted", decided_by="human:credit-officer-7", reviewer="credit-officer-7", shown=[bureau])
    bank.transition(big, "committed", decided_by="agent:sanction-agent@2.0.0")
    print("bank: LN-7733 warranted by a named officer, then committed by the sanction agent")

    # 5. Checkpoints, co-signed by a witness that never sees a record.
    bank.flush()
    witness_state = workdir / "witness-state"
    first = cp.create(export(workdir / "bank.db", "lending"), bank_key, tenant="demo-bank", stream="lending")
    first = cp.cosign(first, witness_key, public)
    witness_state.mkdir()
    cp.save(first, witness_state / "last.json")  # what the witness remembers: the last checkpoint it signed
    with bank.decide("credit.msme.approve", subject="LN-7734") as d:
        d.check(amount=900_000, bureau_score=701)
        d.evidence("bureau_pull", uri="cibil://req/55123", type="tool_call", provider="cibil", content={"score": 701}, retrieved_at=now())
        d.cite(gst_record, keyring=partner_public)
        d.commit("approve")
    bank.flush()
    bank_records = export(workdir / "bank.db", "lending")
    second = cp.create(bank_records, bank_key, tenant="demo-bank", stream="lending", previous=first)
    second = cp.cosign(second, witness_key, public, last_seen=cp.load(witness_state / "last.json"))
    cp.save(second, workdir / "checkpoint.json")
    write_jsonl(bank_records, workdir / "bank-export.jsonl")
    print(f"witness: co-signed the bank's chain at {first['tree_size']} and, with a consistency proof, at {second['tree_size']}")

    # 6. A verifier with two exports and three public keys.
    report = verify_records(bank_records, keyring=public, parent_records=partner_records, checkpoints=[second])[0]
    print(f"verifier: {report.stream} {report.records} records, level {report.level}"
          + (f" ({report.level_reason})" if report.level_reason else ""))
    for note in report.warnings:
        print(f"  note: {note}")

    # 7. The blame walk: from the bank's loan to the partner's GST step.
    for step in trace(loan, bank_records + partner_records, public):
        print("trace: " + step.describe().strip())

    # 8. The bank rewrites one old record and re-seals its whole chain with its own key.
    forged = [dict(r) for r in bank_records]
    forged[1] = {**forged[1], "decision": {**forged[1]["decision"], "summary": "rewritten after the fact"}}
    from warrant.store import seal_record

    prev = None
    for i, record in enumerate(forged):
        body = {k: v for k, v in record.items() if k not in ("seal", "sequence")}
        forged[i] = seal_record(body, i + 1, prev, bank_key)
        prev = forged[i]["seal"]["hash"]
    forged_cp = cp.create(forged, bank_key, tenant="demo-bank", stream="lending")
    try:
        cp.cosign(forged_cp, witness_key, public, last_seen=second)
        print("witness: co-signed the rewritten chain (this must not happen)")
        return 1
    except cp.CheckpointError as exc:
        print(f"witness: refused the rewritten chain: {exc}")

    # 9. Erasure: the partner deletes the GSTN sidecar; its signed record still verifies.
    store = SQLiteStore(workdir / "partner.db")
    store.erase_blob(gstn_digest)
    store.close()
    after = verify_records(export(workdir / "partner.db", "gst"), keyring=public)[0]
    print(f"partner: erased the GSTN salt; chain still {'verifies' if after.ok else 'FAILS'} at {after.level}")

    partner.close()
    bank.close()
    return 0 if report.level == "L3" and after.ok else 1


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="warrant-adr-"))
    sys.exit(main(target))
