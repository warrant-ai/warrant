"""The Agent Decision Record: admissibility, the lifecycle, handoffs, levels, checkpoints, trace."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("celpy")

from warrant import AgentInfo, CitationError, NotWarranted, Warrant
from warrant import checkpoint as cp
from warrant.admissibility import admit, assess, check_history, derive_state, human_linked
from warrant.cli import main
from warrant.policy import PolicyBundle, PolicyError
from warrant.signing import Keyring, SigningKey
from warrant.store import SQLiteStore, seal_record
from warrant.trace import trace
from warrant.verify import verify_records

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "adr"


def _ts(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# -- the seven rules -----------------------------------------------------------------------

OB = {"id": "OB-1", "requires": "tool_call", "kind": "verifiable", "providers": ["cibil"], "max_age_seconds": 86400}
AT = datetime.now(timezone.utc)


def _item(**kw):
    item = {"name": "bureau_pull", "type": "tool_call", "uri": "cibil://1", "content_hash": "a" * 64,
            "provider": "cibil", "retrieved_at": _ts(0.1), "obligation": "OB-1"}
    item.update(kw)
    return {k: v for k, v in item.items() if v is not None}


@pytest.mark.parametrize(
    "item,obligation,reason",
    [
        (_item(provider="self"), OB, "self_attested"),
        (_item(provider="credit-agent"), OB, "self_attested"),
        (_item(provider=None), OB, "self_attested"),
        (_item(provider="experian"), OB, "unqualified_provider"),
        (_item(retrieved_at=_ts(3)), OB, "stale"),
        (_item(retrieved_at=None), OB, "no_timestamp"),
        (_item(content_hash=""), OB, "missing_digest"),
        (_item(type="record", parent={"record_id": "01K5A3Q7Z2XV8M9N4B6C1D0001", "hash": "b" * 64}), {**OB, "requires": "record"}, "parent_not_cited"),
        (_item(type="document"), OB, "wrong_type"),
        (_item(name="other"), {**OB, "name": "bureau_pull"}, "wrong_type"),
    ],
)
def test_each_rule_rejects_with_its_reason(item, obligation, reason):
    admitted, why = admit(item, obligation, actor_name="credit-agent", at=AT, parents={})
    assert (admitted, why) == (False, reason)


def test_admissible_evidence_is_admitted():
    assert admit(_item(), OB, actor_name="credit-agent", at=AT, parents={}) == (True, None)


def test_rules_apply_in_order_so_the_first_failure_is_the_reason():
    # self-attested, unqualified and stale at once: rule 1 is reported
    item = _item(provider="self", retrieved_at=_ts(9))
    assert admit(item, OB, actor_name="a", at=AT, parents={})[1] == "self_attested"


def test_a_cited_parent_counts_only_when_it_was_warranted():
    parent = {"record_id": "01K5A3Q7Z2XV8M9N4B6C1D0001", "hash": "b" * 64}
    item = _item(type="record", content_hash="b" * 64, parent=parent, provider="partner-data")
    ob = {**OB, "requires": "record", "providers": ["partner-data"]}
    ok = admit(item, ob, actor_name="a", at=AT, parents={parent["record_id"]: {**parent, "state": "committed"}})
    pending = admit(item, ob, actor_name="a", at=AT, parents={parent["record_id"]: {**parent, "state": "pending_evidence"}})
    assert ok == (True, None)
    assert pending == (False, "parent_not_warranted")


def test_a_human_must_be_named_and_linked_to_what_they_saw():
    assert human_linked({"shown": ["a" * 64]}, ["a" * 64]) == (False, "unnamed_reviewer")
    assert human_linked({"reviewer": "r", "shown": []}, ["a" * 64]) == (False, "material_not_linked")
    assert human_linked({"reviewer": "r", "shown": ["c" * 64]}, ["a" * 64]) == (False, "material_not_linked")
    assert human_linked({"reviewer": "r", "shown": ["a" * 64]}, ["a" * 64]) == (True, None)


# -- the lifecycle ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,status,unmet,state",
    [
        ("deny", "withheld", [], "refused"),
        ("escalate", "withheld", [], "escalated"),
        ("allow", "withheld", ["OB-1"], "pending_evidence"),
        ("allow", "withheld", [], "warranted"),
        ("allow", "acted", [], "committed"),
    ],
)
def test_the_mandate_and_the_evidence_decide_the_state(result, status, unmet, state):
    record = {"mandate": {"result": result}, "decision": {"status": status}, "obligations": [{"id": "OB-1"}]}
    assert derive_state(record, unmet)[0] == state


def test_committed_is_reachable_only_from_warranted():
    assert check_history(["proposed", "warranted", "committed"]) is None
    assert "illegal" in check_history(["proposed", "pending_evidence", "committed"])
    assert "illegal" in check_history(["proposed", "refused", "warranted"])


def test_an_ordinary_record_has_no_state():
    assert derive_state({"mandate": {"result": "allow"}, "decision": {"status": "acted"}}, []) == (None, [])


# -- the SDK: warrant, commit, enforce --------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    keys = tmp_path / "keys"
    bank_key, partner_key, witness_key = (SigningKey.generate(n) for n in ("demo-bank", "partner-data", "audit-witness"))
    for key in (bank_key, partner_key, witness_key):
        key.save(keys)
    ring = Keyring.load(*(keys / f"{k.issuer}.keys.json" for k in (bank_key, partner_key, witness_key)))
    partner = Warrant("gst", tenant="partner-data", store=tmp_path / "partner.db", signing_key=partner_key,
                      agent=AgentInfo("gst-agent", "1"), policy_bundle=EXAMPLE / "partner-policies", flush_interval=0.02)
    bank = Warrant("lending", tenant="demo-bank", store=tmp_path / "bank.db", signing_key=bank_key,
                   agent=AgentInfo("credit-agent", "3", model="m", runtime="temporal", identity=("npci-agent-registry", "AGT-1")),
                   policy_bundle=EXAMPLE / "policies", flush_interval=0.02)
    yield type("World", (), dict(tmp=tmp_path, bank=bank, partner=partner, ring=ring, bank_key=bank_key,
                                 partner_key=partner_key, witness_key=witness_key))
    bank.close()
    partner.close()


def _records(w, which="bank"):
    client = getattr(w, which)
    client.flush()
    return list(client.store.iter_records())


def _gst(w, *, commit=True, provider="gstn"):
    with w.partner.decide("gst.verify", subject="GSTIN:1") as d:
        d.check(filings_found=True)
        d.evidence("gstn_returns", uri="gstn://1", type="tool_call", provider=provider,
                   content={"turnover_band": "4-5 crore"}, retrieved_at=_ts(0.01), sensitive=True)
        d.claim("gst_turnover_inr", 42_000_000)
        if commit:
            d.commit("verified")
    return _records(w, "partner")[-1]


def _loan(w, parent, subject="LN-1", amount=2_000_000):
    with w.bank.decide("credit.msme.approve", subject=subject) as d:
        d.check(amount=amount, bureau_score=742)
        d.evidence("bureau_pull", uri="cibil://1", type="tool_call", provider="cibil", content={"s": 742}, retrieved_at=_ts(0.01))
        d.cite(parent, keyring=w.ring)
        d.commit("approve")
    return d.record_id


def test_a_warranted_decision_commits_and_the_record_says_why(world):
    gst = _gst(world)
    rid = _loan(world, gst)
    record = [r for r in _records(world) if r["record_id"] == rid][0]
    assert record["verdict"]["state"] == "committed"
    assert record["verdict"]["met"] == ["OB-1", "OB-2"]
    assert [h["state"] for h in record["verdict"]["history"]] == ["proposed", "warranted", "committed"]
    assert record["verdict"]["decided_by"] == "policy:CR-09@2026.10"
    assert record["parents"][0]["hash"] == gst["seal"]["hash"] and record["parents"][0]["issuer"] == "partner-data"
    assert all(e["admission"]["status"] == "admitted" for e in record["evidence"] if "obligation" in e)
    assert record["actor"]["identity"] == {"registry": "npci-agent-registry", "id": "AGT-1"}
    assert record["retention"]["class"] == "rbi-credit-8y" and record["retention"]["retain_until"] > record["timestamp"]
    assert "claims" not in record["decision"], "a parent's claims are never copied"


def test_commit_fails_closed_and_the_withheld_record_says_what_was_missing(world):
    with pytest.raises(NotWarranted) as info:
        with world.bank.decide("credit.msme.approve", subject="LN-2") as d:
            d.check(amount=2_000_000, bureau_score=742)
            d.evidence("partner says ok", uri="msg://1", type="record", provider="self", content={"ok": True}, obligation="OB-2")
            d.commit("approve")
    assert info.value.state == "pending_evidence" and set(info.value.unmet) == {"OB-1", "OB-2"}
    record = _records(world)[-1]
    assert record["decision"]["status"] == "failed"
    rejected = [e for e in record["evidence"] if e.get("admission", {}).get("status") == "rejected"]
    assert rejected[0]["admission"]["reason"] == "self_attested"


def test_an_enforcing_policy_blocks_plain_act_too(world):
    with pytest.raises(NotWarranted):
        with world.bank.decide("credit.msme.approve", subject="LN-3") as d:
            d.check(amount=2_000_000, bureau_score=742)
            d.act("approve")


def test_without_enforcement_acting_early_is_recorded_honestly_and_caps_the_level(tmp_path):
    key = SigningKey.generate("demo-bank")
    w = Warrant("lending", tenant="demo-bank", store=tmp_path / "r.db", signing_key=key, agent=AgentInfo("a", "1"), flush_interval=0.02)
    with w.decide("credit.approve", subject="LN-4") as d:
        d.obligation("OB-1", requires="tool_call", providers=["cibil"])
        d.act("approve")
    w.flush()
    records = list(w.store.iter_records())
    w.close()
    assert records[0]["verdict"]["state"] == "pending_evidence"
    report = verify_records(records, keyring=Keyring([key.public]))[0]
    assert report.ok and report.level == "L1"
    assert any("acted without a warrant" in n for n in report.warnings)


def test_a_conditional_obligation_applies_only_when_its_condition_holds(world):
    gst = _gst(world)
    with world.bank.decide("credit.msme.approve", subject="LN-5") as d:
        d.check(amount=3_500_000, bureau_score=742)
        d.evidence("bureau_pull", uri="cibil://1", type="tool_call", provider="cibil", content={}, retrieved_at=_ts(0.01))
        d.cite(gst, keyring=world.ring)
        state = d.warrant()
    assert state.state == "pending_evidence" and state.unmet == ("OB-3",)


def test_a_human_transition_warrants_and_a_second_commits(world):
    gst = _gst(world)
    with world.bank.decide("credit.msme.approve", subject="LN-6") as d:
        d.check(amount=3_500_000, bureau_score=742)
        shown = d.evidence("bureau_pull", uri="cibil://1", type="tool_call", provider="cibil", content={}, retrieved_at=_ts(0.01))
        d.cite(gst, keyring=world.ring)
    rid = d.record_id
    with pytest.raises(ValueError, match="named reviewer"):
        world.bank.transition(rid, "warranted", decided_by="human:x", reviewer="x", shown=["f" * 64])
    with pytest.raises(ValueError, match="illegal"):
        world.bank.transition(rid, "committed", decided_by="agent:sanction")
    world.bank.transition(rid, "warranted", decided_by="human:officer", reviewer="officer", shown=[shown])
    world.bank.transition(rid, "committed", decided_by="agent:sanction")
    with pytest.raises(ValueError, match="illegal"):
        world.bank.transition(rid, "refused", decided_by="human:officer")
    records = _records(world)
    report = verify_records(records, keyring=world.ring, parent_records=_records(world, "partner"))[0]
    assert report.ok and report.level == "L2", report.warnings


def test_a_transition_cannot_add_missing_evidence(world):
    with world.bank.decide("credit.msme.approve", subject="LN-7") as d:
        d.check(amount=2_000_000, bureau_score=742)
    with pytest.raises(ValueError, match="need evidence"):
        world.bank.transition(d.record_id, "warranted", decided_by="human:x", reviewer="x", shown=[])


def test_citing_an_altered_or_foreign_signed_record_is_refused(world):
    gst = _gst(world)
    tampered = {**gst, "decision": {**gst["decision"], "summary": "edited"}}
    with world.bank.decide("credit.msme.approve", subject="LN-8") as d:
        with pytest.raises(CitationError, match="altered"):
            d.cite(tampered)
        with pytest.raises(CitationError, match="unknown key"):
            d.cite(gst, keyring=Keyring([world.bank_key.public]))
        with pytest.raises(CitationError, match="not sealed"):
            d.cite({"record_id": gst["record_id"]})


def test_sensitive_evidence_keeps_its_salt_in_the_sidecar_and_erasure_leaves_the_record_verifying(world):
    gst = _gst(world)
    item = [e for e in gst["evidence"] if e["name"] == "gstn_returns"][0]
    assert item["salted"] is True and "excerpt" not in item
    store = world.partner.store
    assert len(store.get_blob(item["content_hash"])["salt"]) == 64
    assert store.erase_blob(item["content_hash"]) is True
    assert store.get_blob(item["content_hash"]) is None
    assert verify_records(_records(world, "partner"), keyring=world.ring)[0].ok


def test_sensitive_evidence_refuses_an_excerpt_or_a_bare_hash(world):
    with world.bank.decide("credit.msme.approve", subject="LN-9") as d:
        with pytest.raises(ValueError, match="excerpt"):
            d.evidence("pan", uri="x://1", content="ABCDE1234F", excerpt="ABCDE1234F", sensitive=True)
        with pytest.raises(ValueError, match="bare hash"):
            d.evidence("pan", uri="x://1", content_hash="a" * 64, sensitive=True)


def test_a_signing_key_with_nowhere_to_seal_is_refused(tmp_path):
    with pytest.raises(ValueError, match="collector"):
        Warrant("s", store="https://collector.invalid", signing_key=SigningKey.generate("x"), agent=AgentInfo("a", "1"))


# -- policies --------------------------------------------------------------------------------


def _policy(tmp_path, obligations):
    (tmp_path / "p.yaml").write_text(
        "policy_id: P\nversion: '1'\nclasses: [c.x]\nclauses:\n  - {id: '1', when: 'true', result: allow}\n" + obligations)
    return tmp_path / "p.yaml"


@pytest.mark.parametrize(
    "obligations,message",
    [
        ("obligations:\n  - {id: A, requires: tool_call, providers: [self]}\n", "cannot be a qualified provider"),
        ("obligations:\n  - {id: A, requires: tool_call, max_age: soon}\n", "not a duration"),
        ("obligations:\n  - {id: A, requires: tool_call, clause: '9'}\n", "not a clause"),
        ("obligations:\n  - {id: A, requires: telepathy}\n", "requires must be"),
        ("obligations:\n  - {id: A, requires: tool_call}\n  - {id: A, requires: document}\n", "duplicate obligation"),
    ],
)
def test_malformed_obligations_are_load_errors(tmp_path, obligations, message):
    with pytest.raises(PolicyError, match=message):
        PolicyBundle.load(_policy(tmp_path, obligations))


# -- verification levels ------------------------------------------------------------------


def test_levels_rise_with_what_the_verifier_is_given(world):
    gst = _gst(world)
    _loan(world, gst)
    records, partner = _records(world), _records(world, "partner")
    assert verify_records(records)[0].level == "below L1"
    assert verify_records(records, keyring=world.ring)[0].level == "L1"  # parent not supplied
    assert verify_records(records, keyring=world.ring, parent_records=partner)[0].level == "L2"
    checkpoint = cp.cosign(cp.create(records, world.bank_key, tenant="demo-bank", stream="lending"), world.witness_key, world.ring)
    assert verify_records(records, keyring=world.ring, parent_records=partner, checkpoints=[checkpoint])[0].level == "L3"
    self_witnessed = cp.create(records, world.bank_key, tenant="demo-bank", stream="lending")
    assert verify_records(records, keyring=world.ring, parent_records=partner, checkpoints=[self_witnessed])[0].level == "L2"


def test_a_verdict_the_rules_do_not_support_is_found_even_when_resealed(world):
    gst = _gst(world)
    _loan(world, gst)
    with world.bank.decide("credit.msme.approve", subject="LN-10") as d:
        d.check(amount=2_000_000, bureau_score=742)
    records = _records(world)
    forged, prev = [], None
    for i, record in enumerate(records):
        body = {k: v for k, v in record.items() if k not in ("seal", "sequence")}
        if body["decision"]["subject"] == "LN-10":
            body["verdict"] = {**body["verdict"], "state": "warranted", "unmet": [], "history": [{"state": "proposed", "at": body["verdict"]["at"]}, {"state": "warranted", "at": body["verdict"]["at"]}]}
        sealed = seal_record(body, i + 1, prev, world.bank_key)
        forged.append(sealed)
        prev = sealed["seal"]["hash"]
    report = verify_records(forged, keyring=world.ring, parent_records=_records(world, "partner"))[0]
    assert report.ok and report.level == "L1"
    assert any("give 'pending_evidence'" in n for n in report.warnings)


def test_a_tampered_signed_record_is_an_error(world):
    _gst(world)
    records = _records(world, "partner")
    records[0] = {**records[0], "decision": {**records[0]["decision"], "action": "rejected"}}
    report = verify_records(records, keyring=world.ring)[0]
    assert not report.ok and report.level == "below L1"


# -- checkpoints -------------------------------------------------------------------------------


def test_a_witness_refuses_a_shrunk_or_rewritten_chain_and_accepts_an_honest_append(world):
    gst = _gst(world)
    _loan(world, gst, "LN-11")
    _loan(world, gst, "LN-11b")
    first_records = _records(world)
    first = cp.cosign(cp.create(first_records, world.bank_key, tenant="demo-bank", stream="lending"), world.witness_key, world.ring)
    _loan(world, gst, "LN-12")
    records = _records(world)
    honest = cp.create(records, world.bank_key, tenant="demo-bank", stream="lending", previous=first)
    assert cp.cosign(honest, world.witness_key, world.ring, last_seen=first)["cosignatures"]

    without_proof = cp.create(records, world.bank_key, tenant="demo-bank", stream="lending")
    with pytest.raises(cp.CheckpointError, match="no consistency proof"):
        cp.cosign(without_proof, world.witness_key, world.ring, last_seen=first)

    # rewrite the first record, re-seal everything, append, and forge a "consistency" section
    forged, prev = [], None
    for i, record in enumerate(records):
        body = {k: v for k, v in record.items() if k not in ("seal", "sequence")}
        if i == 0:
            body["decision"] = {**body["decision"], "summary": "rewritten"}
        forged.append(seal_record(body, i + 1, prev, world.bank_key))
        prev = forged[-1]["seal"]["hash"]
    rewritten = cp.create(forged, world.bank_key, tenant="demo-bank", stream="lending")
    rewritten["consistency"] = {**honest["consistency"]}
    with pytest.raises(cp.CheckpointError):
        cp.cosign(rewritten, world.witness_key, world.ring, last_seen=first)

    shrunk = cp.create(records[:1], world.bank_key, tenant="demo-bank", stream="lending")
    with pytest.raises(cp.CheckpointError, match="shrank"):
        cp.cosign(shrunk, world.witness_key, world.ring, last_seen=first)

    with pytest.raises(cp.CheckpointError, match="own checkpoint"):
        cp.cosign(honest, world.bank_key, world.ring)


def test_an_inclusion_proof_shows_one_record_without_the_rest(world):
    gst = _gst(world)
    _loan(world, gst)
    records = _records(world)
    checkpoint = cp.create(records, world.bank_key, tenant="demo-bank", stream="lending")
    proof = cp.prove_inclusion(records, checkpoint, records[0]["record_id"])
    assert cp.check_inclusion(proof, records[0])
    assert not cp.check_inclusion({**proof, "index": proof["index"] + 1} if len(records) > 1 else {**proof, "root": "0" * 64}, records[0])


def test_anchoring_writes_a_pending_opentimestamps_proof_per_calendar(world, tmp_path):
    _gst(world)
    checkpoint = cp.create(_records(world, "partner"), world.partner_key, tenant="partner-data", stream="gst")
    calls = []

    def post(url, body):
        calls.append((url, body))
        if "down" in url:
            raise OSError("unreachable")
        return b"\xf0\x10" + b"\x00" * 16 + b"\x08\x00\x83\xdf\xe3\x0dx"

    out = cp.anchor(checkpoint, tmp_path, calendars=["https://a.example", "https://down.example"], post=post)
    assert calls[0] == ("https://a.example/digest", bytes.fromhex(checkpoint["root"]))
    assert len(out["anchors"]) == 1 and out["anchors"][0]["status"] == "pending"
    data = (tmp_path / out["anchors"][0]["file"]).read_bytes()
    assert data.startswith(cp.OTS_MAGIC + b"\x01\x08" + bytes.fromhex(checkpoint["root"]))
    with pytest.raises(cp.CheckpointError, match="no calendar"):
        cp.anchor(checkpoint, tmp_path, calendars=["https://down.example"], post=post)


# -- trace -----------------------------------------------------------------------------------


def test_trace_walks_upstream_to_the_partners_step(world):
    gst = _gst(world)
    rid = _loan(world, gst)
    steps = trace(rid, _records(world) + _records(world, "partner"), world.ring)
    assert [s.issuer for s in steps] == ["partner-data", "demo-bank"]
    assert not any(s.problems for s in steps)


def test_trace_finds_a_citation_the_upstream_issuer_never_warranted(world):
    gst = _gst(world, commit=False, provider="self")  # the partner's own record: pending_evidence
    cited_as_warranted = {**gst}
    with world.bank.decide("credit.msme.approve", subject="LN-13") as d:
        d.check(amount=2_000_000, bureau_score=742)
        d.cite(cited_as_warranted, keyring=world.ring, state="warranted")  # the bank's agent misstates it
    steps = trace(d.record_id, _records(world) + _records(world, "partner"), world.ring)
    bank_step = [s for s in steps if s.issuer == "demo-bank"][0]
    assert any("relied on" in p for p in bank_step.problems)


# -- the command line -------------------------------------------------------------------------


def test_the_cli_covers_keys_checkpoints_verify_trace_evidence_and_erase(world, capsys):
    gst = _gst(world)
    rid = _loan(world, gst)
    tmp = world.tmp
    bank_export, partner_export = tmp / "bank.jsonl", tmp / "partner.jsonl"
    bank_export.write_text("".join(json.dumps(r) + "\n" for r in _records(world)))
    partner_export.write_text("".join(json.dumps(r) + "\n" for r in _records(world, "partner")))
    keys = [str(tmp / "keys" / f"{n}.keys.json") for n in ("demo-bank", "partner-data", "audit-witness")]
    keyargs = sum((["--keys", k] for k in keys), [])

    assert main(["keys", "generate", "--issuer", "another", "--out", str(tmp / "k2")]) == 0
    assert main(["keys", "show", str(tmp / "k2" / "another.key")]) == 0
    assert main(["checkpoint", "create", "--export", str(bank_export), "--stream", "lending", "--key", str(tmp / "keys" / "demo-bank.key"), "-o", str(tmp / "cp.json")]) == 0
    assert main(["checkpoint", "cosign", str(tmp / "cp.json"), "--key", str(tmp / "keys" / "audit-witness.key"), "--keys", keys[0], "--keys", keys[2], "--state", str(tmp / "ws")]) == 0
    assert main(["checkpoint", "verify", str(tmp / "cp.json"), "--export", str(bank_export), *keyargs]) == 0
    assert main(["checkpoint", "prove", str(tmp / "cp.json"), "--export", str(bank_export), "--record", rid, "-o", str(tmp / "proof.json")]) == 0
    assert main(["verify", str(bank_export), *keyargs, "--parents", str(partner_export), "--checkpoint", str(tmp / "cp.json"), "--require-level", "L3"]) == 0
    assert main(["verify", str(bank_export), *keyargs, "--require-level", "L2"]) == 1  # parent not supplied
    assert main(["trace", rid, "--export", str(bank_export), "--export", str(partner_export), *keyargs]) == 0
    out = capsys.readouterr().out
    assert "L3" in out and "partner-data" in out

    item = [e for e in gst["evidence"] if e["name"] == "gstn_returns"][0]
    salt = world.partner.store.get_blob(item["content_hash"])["salt"]
    artefact = tmp / "gstn.json"
    artefact.write_text(json.dumps({"turnover_band": "4-5 crore"}))
    assert main(["evidence", "check", "--hash", item["content_hash"], "--file", str(artefact), "--json", "--salt", salt]) == 0
    assert main(["evidence", "check", "--hash", item["content_hash"], "--file", str(artefact), "--json"]) == 1
    world.partner.flush()
    assert main(["erase", "--store", str(tmp / "partner.db"), "--hash", item["content_hash"]]) == 0


def test_the_worked_example_runs_to_level_three(tmp_path):
    result = subprocess.run([sys.executable, str(EXAMPLE / "run_adr.py"), str(tmp_path / "run")], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "level L3" in result.stdout
    assert "refused the rewritten chain" in result.stdout
    assert "refused to act" in result.stdout


def test_the_pack_states_its_limits_for_the_level_it_reached(world, tmp_path):
    from warrant.pack import build_pack

    gst = _gst(world)
    _loan(world, gst)
    records = _records(world)
    keys = [world.tmp / "keys" / f"{n}.keys.json" for n in ("demo-bank", "partner-data", "audit-witness")]
    partner_export = tmp_path / "partner.jsonl"
    partner_export.write_text("".join(json.dumps(r) + "\n" for r in _records(world, "partner")))
    checkpoint = cp.cosign(cp.create(records, world.bank_key, tenant="demo-bank", stream="lending"), world.witness_key, world.ring)
    cp.save(checkpoint, tmp_path / "cp.json")

    plain = build_pack(world.bank.store, tmp_path / "plain", stream="lending")
    assert "could not have re-sealed" in (plain.directory / "README.md").read_text()
    signed = build_pack(world.bank.store, tmp_path / "signed", stream="lending", keys=keys, parents=[partner_export])
    text = (signed.directory / "README.md").read_text()
    assert "L2" in text and "rewritten its own history" in text and "--keys keys/demo-bank.keys.json" in text
    attested = build_pack(world.bank.store, tmp_path / "attested", stream="lending", keys=keys, parents=[partner_export], checkpoints=[tmp_path / "cp.json"])
    text = (attested.directory / "README.md").read_text()
    assert "L3" in text and "audit-witness" in text
    assert (attested.directory / "checkpoints" / "cp.json").exists() and (attested.directory / "keys" / "demo-bank.keys.json").exists()
    assert main(["verify", str(attested.directory / "records.jsonl"), *sum((["--keys", str(attested.directory / "keys" / k.name)] for k in keys), []),
                 "--parents", str(attested.directory / "parents" / "partner.jsonl"), "--checkpoint", str(attested.directory / "checkpoints" / "cp.json"), "--require-level", "L3"]) == 0


def test_a_citation_without_a_verified_signature_cannot_authorise(world):
    gst = _gst(world)
    with world.bank.decide("credit.msme.approve", subject="LN-14") as d:
        d.check(amount=2_000_000, bureau_score=742)
        d.evidence("bureau_pull", uri="cibil://1", type="tool_call", provider="cibil", content={}, retrieved_at=_ts(0.01))
        d.cite(gst)  # no keyring: recorded, but names no provider
        state = d.warrant()
    assert state.state == "pending_evidence" and state.unmet == ("OB-2",)
    assert ("partner-data:gst.verify", "self_attested") in state.rejected


def test_an_identity_can_carry_a_registry_uri(tmp_path):
    w = Warrant("s", tenant="t", store=tmp_path / "r.db", agent=AgentInfo("a", "1", identity=("npci-agent-registry", "AGT-1", "https://registry.example/AGT-1")), flush_interval=0.02)
    with w.decide("credit.approve", subject="x") as d:
        d.act("approve")
    w.flush()
    record = next(w.store.iter_records())
    w.close()
    assert record["actor"]["identity"]["uri"] == "https://registry.example/AGT-1"


def test_a_collector_client_cannot_warrant_a_decision_missing_evidence(world):
    """Without a store the decision record must be passed, so the evidence rule still applies."""
    with world.bank.decide("credit.msme.approve", subject="LN-15") as d:
        d.check(amount=2_000_000, bureau_score=742)
    decision = [r for r in _records(world) if r["record_id"] == d.record_id][0]
    sent = []

    class Collector:
        def write(self, records):
            sent.extend(records)

    remote = Warrant("lending", tenant="demo-bank", store=Collector(), agent=AgentInfo("credit-agent", "3"), flush_interval=0.02)
    try:
        with pytest.raises(ValueError, match="needs the decision record"):
            remote.transition(d.record_id, "warranted", decided_by="human:x", from_state="pending_evidence", reviewer="x", shown=[])
        with pytest.raises(ValueError, match="need evidence"):
            remote.transition(d.record_id, "warranted", decided_by="human:x", from_state="pending_evidence", reviewer="x", shown=[], decision=decision)
    finally:
        remote.close()


def test_a_reviewer_whose_approval_is_the_decision_meets_the_human_obligation(tmp_path):
    w = Warrant("s", tenant="t", store=tmp_path / "r.db", agent=AgentInfo("engine", "1"), flush_interval=0.02)
    with w.decide("frw.withholding.determination", subject="FRW-1") as d:
        d.obligation("OB-REVIEW", requires="human_review")
        seen = d.evidence("trc", uri="frw://case/1/trc", type="document", provider="foreign-tax-authority", content="trc")
        d.human_review("asha@example.test", shown=[seen], note="treaty rate agreed")
        state = d.warrant()
    assert state.state == "warranted" and state.met == ("OB-REVIEW",)
    with w.decide("frw.withholding.determination", subject="FRW-2") as d:
        with pytest.raises(ValueError, match="sha256"):
            d.human_review("asha@example.test", shown=["not-a-digest"])
        with pytest.raises(ValueError, match="verdict"):
            d.human_review("asha@example.test", shown=[], verdict="maybe")
    w.flush()
    record = [r for r in w.store.iter_records() if r["decision"]["subject"] == "FRW-1"][0]
    w.close()
    assert record["human"]["reviewer"] == "asha@example.test" and record["human"]["shown"] == [seen]
    assert record["verdict"]["state"] == "warranted"


def test_an_item_withheld_from_offer_is_never_matched_to_an_obligation(tmp_path):
    w = Warrant("s", tenant="t", store=tmp_path / "r.db", agent=AgentInfo("engine", "1"), flush_interval=0.02)

    def decide(subject, **offer):
        with w.decide("frw.withholding.determination", subject=subject) as d:
            d.obligation("OB-TRC", requires="document", providers=["foreign-tax-authority"], name="trc")
            d.obligation("OB-INV", requires="tool_call", providers=["erp"])
            d.evidence("trc", uri="frw://case/1/trc", type="document", provider="foreign-tax-authority", content="trc", **offer)
            d.evidence("invoice", uri="frw://case/1/invoice", type="tool_call", provider="erp", content="inv", **offer)
            return d.warrant()

    matched = decide("FRW-1")
    assert matched.state == "warranted" and set(matched.met) == {"OB-TRC", "OB-INV"}, "by name, and by type"
    withheld = decide("FRW-2", offer=False)
    assert withheld.state == "pending_evidence" and set(withheld.unmet) == {"OB-TRC", "OB-INV"}
    with w.decide("frw.withholding.determination", subject="FRW-3") as d:
        with pytest.raises(ValueError, match="withheld from offer"):
            d.evidence("trc", uri="frw://case/1/trc", type="document", content="trc", obligation="OB-TRC", offer=False)
    w.flush()
    record = [r for r in w.store.iter_records() if r["decision"]["subject"] == "FRW-2"][0]
    w.close()
    assert len(record["evidence"]) == 2, "still on the record, as inputs"
    assert all("obligation" not in e and "admission" not in e for e in record["evidence"])
    assert all(o["met"] is False for o in record["obligations"])
