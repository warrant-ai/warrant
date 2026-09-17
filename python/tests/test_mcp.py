import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")
pytest.importorskip("celpy")

from mcp import Client  # noqa: E402

from warrant import AgentInfo, SQLiteStore, Warrant, verify_records  # noqa: E402
from warrant.mcp_server import MAX_EVIDENCE, create_server  # noqa: E402

POLICIES = Path(__file__).parent.parent.parent / "examples" / "policies"
GOOD = {"amount": 450000, "bureau_score": 748, "foir": 0.38}


def session(tmp_path, calls, *, policy=True, **options):
    """Run ``calls(client)`` against an in-process server; return what it returned and the stored records."""
    db = tmp_path / "records.db"
    w = Warrant("lending", tenant="demo-bank", store=db, agent=AgentInfo("credit-underwriter", "2.4.0"),
                policy_bundle=POLICIES if policy else None, currency="INR", flush_interval=0.02, **options)

    async def run():
        async with Client(create_server(w)) as client:
            return await calls(client)

    try:
        result = asyncio.run(run())
        assert w.flush(10)
    finally:
        w.close()
    store = SQLiteStore(db, read_only=True)
    records = list(store.iter_records())
    store.close()
    return result, records


async def call(client, tool, **arguments):
    result = await client.call_tool(tool, arguments)
    text = result.content[0].text
    return (None, text) if result.is_error else (json.loads(text), None)


def test_the_server_describes_itself_and_the_mandate(tmp_path):
    async def calls(client):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        governed, _ = await call(client, "describe_mandate", decision_class="credit.approve")
        free, _ = await call(client, "describe_mandate", decision_class="kyc.verify")
        return tools, governed, free

    (tools, governed, free), records = session(tmp_path, calls)
    assert sorted(tools) == ["check_mandate", "describe_mandate", "record_decision", "record_outcome"]
    assert not any("verdict" in name or "human" in name for name in tools), "an agent must not be able to record a human verdict"
    assert "you cannot supply it" in tools["record_decision"].description
    assert (governed["policy_id"], governed["policy_version"], governed["default"], governed["fail_mode"]) == ("CR-07", "2026.3", "escalate", "closed")
    assert [c["id"] for c in governed["clauses"]] == ["4.1", "4.2", "4.3"] and "double(foir)" in governed["clauses"][1]["when"]
    assert free["governed"] is False
    assert records == []


def test_check_then_act_then_record_then_outcome(tmp_path):
    async def calls(client):
        verdict, _ = await call(client, "check_mandate", decision_class="credit.approve", inputs=GOOD)
        recorded, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-1", inputs=GOOD, action="approve",
                                 summary="Within limit.", cost_centre="retail-lending", on_behalf_of="branch:jayanagar",
                                 evidence=[{"name": "bureau_pull", "uri": "cibil://req/1", "type": "tool_call", "content": {"score": 748}}],
                                 model_calls=[{"provider": "anthropic", "model": "claude-sonnet-5", "tokens_in": 1200, "tokens_out": 300, "amount": 3.5}], cost=0.34)
        outcome, _ = await call(client, "record_outcome", label="performing", decision_record_id=recorded["record_id"], source="lms://x")
        by_subject, _ = await call(client, "record_outcome", label="default", subject="LN-1")
        return verdict, recorded, outcome, by_subject

    (verdict, recorded, outcome, by_subject), records = session(tmp_path, calls, capture_inputs=True)
    assert verdict == {"result": "allow", "allowed": True, "policy_id": "CR-07", "policy_version": "2026.3", "clause": "4.2", "reason": verdict["reason"]}
    assert recorded["status"] == "acted" and recorded["outside_mandate"] is False and recorded["mandate"] == verdict
    decision, first, second = records
    assert decision["record_id"] == recorded["record_id"]
    assert decision["actor"] == {"name": "credit-underwriter", "version": "2.4.0", "on_behalf_of": "branch:jayanagar"}
    assert decision["decision"] == {"class": "credit.approve", "action": "approve", "subject": "LN-1", "status": "acted", "summary": "Within limit.", "inputs": GOOD}
    assert decision["mandate"]["clause"] == "4.2" and decision["cost"]["amount"] == 3.84 and decision["cost"]["cost_centre"] == "retail-lending"
    assert [e["name"] for e in decision["evidence"]] == ["bureau_pull", "anthropic/claude-sonnet-5"] and "content" not in decision["evidence"][0]
    assert first["references"]["decision_record_id"] == second["references"]["decision_record_id"] == recorded["record_id"]
    assert (first["outcome"]["label"], second["outcome"]["label"]) == ("performing", "default") and outcome["record_id"] == first["record_id"]
    assert all(r.ok for r in verify_records(records))


def test_the_mandate_is_evaluated_by_the_server_not_claimed_by_the_agent(tmp_path):
    big = {"amount": 900000, "bureau_score": 790, "foir": 0.30}

    async def calls(client):
        acted, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-2", inputs=big, action="approve")
        held, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-3", inputs=big, require_human=True, human_note="over limit")
        missing, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-4", inputs={"amount": 1}, action="approve")
        smuggled = await client.call_tool("record_decision", {"decision_class": "credit.approve", "subject": "LN-5", "inputs": big, "action": "approve", "mandate": {"result": "allow"}, "actor": {"name": "cfo"}})
        return acted, held, missing, smuggled

    (acted, held, missing, smuggled), records = session(tmp_path, calls)
    assert acted["outside_mandate"] is True and acted["mandate"]["result"] == "escalate" and "queued for human review" in acted["detail"]
    assert held == {"record_id": held["record_id"], "status": "withheld", "mandate": held["mandate"], "outside_mandate": False}
    assert missing["mandate"]["result"] == "deny" and missing["mandate"]["flagged"] is True and missing["outside_mandate"] is True
    by_subject = {r["decision"]["subject"]: r for r in records}
    assert (by_subject["LN-2"]["decision"]["status"], by_subject["LN-2"]["mandate"]["result"]) == ("acted", "escalate")
    assert by_subject["LN-3"]["human"] == {"required": True, "note": "over limit"}
    # Arguments the tool does not declare never reach the record, whether the SDK drops or rejects them.
    if not smuggled.is_error:
        assert by_subject["LN-5"]["mandate"]["result"] == "escalate" and by_subject["LN-5"]["actor"]["name"] == "credit-underwriter"
    assert all(r["actor"]["name"] == "credit-underwriter" for r in records)


def test_without_a_policy_everything_is_unchecked_and_never_allowed(tmp_path):
    async def calls(client):
        verdict, _ = await call(client, "check_mandate", decision_class="credit.approve", inputs=GOOD)
        recorded, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-6", action="approve")
        return verdict, recorded

    (verdict, recorded), records = session(tmp_path, calls, policy=False)
    assert verdict == {"result": "unchecked", "allowed": False, "reason": "no policy engine configured"}
    assert recorded["outside_mandate"] is False and records[0]["mandate"]["result"] == "unchecked"


def test_a_malformed_call_is_refused_with_a_reason_and_records_nothing(tmp_path):
    bad = [
        ({"decision_class": "Credit Approve", "subject": "x"}, "decision_class must look like"),
        ({"decision_class": "credit.approve", "subject": " "}, "subject must be a non-empty string"),
        ({"decision_class": "credit.approve", "subject": "x", "inputs": {"self": 1}}, "'self' cannot be used"),
        ({"decision_class": "credit.approve", "subject": "x", "inputs": {"blob": "x" * 70000}}, "the limit is 65536"),
        ({"decision_class": "credit.approve", "subject": "x", "evidence": [{"name": "e", "uri": "x://y"}]}, "needs content"),
        ({"decision_class": "credit.approve", "subject": "x", "evidence": [{"name": "e", "uri": "x://y", "content_hash": "abc"}]}, "64 lowercase hex"),
        ({"decision_class": "credit.approve", "subject": "x", "evidence": [{"name": "e", "uri": "x://y", "content": "c", "type": "pdf"}]}, "type must be one of"),
        ({"decision_class": "credit.approve", "subject": "x", "evidence": [{"name": "e", "uri": "x://y", "content": "c"}] * (MAX_EVIDENCE + 1)}, "at most 100 evidence items"),
        ({"decision_class": "credit.approve", "subject": "x", "model_calls": [{"provider": "a", "model": "m", "tokens_in": -1}]}, "tokens_in must be a whole number"),
        ({"decision_class": "credit.approve", "subject": "x", "cost": -2}, "cost must be a number of zero or more"),
    ]

    async def calls(client):
        errors = [(await call(client, "record_decision", **arguments))[1] for arguments, _ in bad]
        errors.append((await call(client, "record_outcome", label="default", subject="LN-404"))[1])
        errors.append((await call(client, "record_outcome", label="default"))[1])
        return errors

    errors, records = session(tmp_path, calls)
    for (_, expected), got in zip(bad, errors):
        assert got is not None and expected in got, (expected, got)
    assert "no decision for subject 'LN-404'" in errors[-2] and "pass subject or decision_record_id" in errors[-1]
    assert records == [], "a malformed tool call is not a decision and must not reach the ledger"


def test_cli_refuses_a_missing_policy_bundle(tmp_path, capsys):
    from warrant.cli import main

    assert main(["mcp", "--stream", "lending", "--store", str(tmp_path / "r.db"), "--policy", str(tmp_path / "nope")]) == 2
    assert "policy bundle not found" in capsys.readouterr().err


def test_over_stdio_as_an_agent_host_would_launch_it(tmp_path):
    import sys

    from mcp import StdioServerParameters

    db = tmp_path / "records.db"
    params = StdioServerParameters(command=sys.executable, args=["-m", "warrant.cli", "mcp", "--stream", "lending", "--store", str(db), "--policy", str(POLICIES),
                                                                 "--agent-name", "credit-underwriter", "--agent-version", "2.4.0", "--tenant", "demo-bank", "--currency", "INR"])

    async def run():
        async with Client(params) as client:
            verdict, _ = await call(client, "check_mandate", decision_class="credit.approve", inputs=GOOD)
            recorded, _ = await call(client, "record_decision", decision_class="credit.approve", subject="LN-9", inputs=GOOD, action="approve")
            return verdict, recorded

    verdict, recorded = asyncio.run(run())
    assert verdict["allowed"] is True and recorded["status"] == "acted"
    store = SQLiteStore(db, read_only=True)  # the server flushed on exit
    records = list(store.iter_records())
    store.close()
    assert [r["record_id"] for r in records] == [recorded["record_id"]] and records[0]["tenant"] == "demo-bank"
    assert all(r.ok for r in verify_records(records))
