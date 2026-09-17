import asyncio
from pathlib import Path

import pytest

pytest.importorskip("celpy")

from warrant import AgentInfo, SQLiteStore, Warrant, verify_records  # noqa: E402
from warrant.adapters import AdapterError, ToolDecision  # noqa: E402

POLICIES = Path(__file__).parent.parent.parent / "examples" / "policies"
GOOD = {"loan_id": "LN-1", "amount": 450000, "bureau_score": 748, "foir": 0.38}
BIG = {"loan_id": "LN-2", "amount": 900000, "bureau_score": 790, "foir": 0.30}
WEAK = {"loan_id": "LN-3", "amount": 100000, "bureau_score": 610, "foir": 0.20}
APPROVE = ToolDecision("credit.approve", subject="loan_id", action="approve", inputs=["amount", "bureau_score", "foir"], cost_centre="retail-lending")


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "records.db"
    w = Warrant("lending", tenant="demo-bank", store=db, agent=AgentInfo("credit-underwriter", "2.4.0"), policy_bundle=POLICIES, currency="INR", flush_interval=0.02)

    def records():
        assert w.flush(10)
        store = SQLiteStore(db, read_only=True)
        try:
            return list(store.iter_records())
        finally:
            store.close()

    yield w, records
    w.close()


# -- the mapping ----------------------------------------------------------------


def test_tool_decision_reads_subject_and_inputs():
    assert APPROVE.read("approve_loan", {**GOOD, "note": "x"}) == ("LN-1", {"amount": 450000, "bureau_score": 748, "foir": 0.38})
    everything = ToolDecision("credit.approve", subject=lambda a: f"LN-{a['n']}", inputs=None)
    assert everything.read("t", {"n": 7}) == ("LN-7", {"n": 7})
    assert ToolDecision("c.d", subject="id").read("t", {"id": 42})[0] == "42"
    for mapping, args, message in [
        (APPROVE, {"amount": 1}, "no subject; expected a non-empty 'loan_id'"),
        (APPROVE, "not an object", "tool arguments are not an object"),
        (ToolDecision("c.d", subject="id"), {"id": "x", "self": 1}, "'self' cannot be used"),
        (ToolDecision("c.d", subject="id", inputs=lambda a: {"when": object()}), {"id": "x"}, "policy inputs must be JSON values"),
        (ToolDecision("c.d", subject=lambda a: a["missing"]), {}, "could not read the decision from the tool arguments: KeyError"),
    ]:
        with pytest.raises(AdapterError, match=message):
            mapping.read("t", args)


# -- LangGraph ------------------------------------------------------------------


def _graph(guard, *, checkpointer=None):
    pytest.importorskip("langgraph")
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    ran = []

    @tool
    def approve_loan(loan_id: str, amount: int, bureau_score: int, foir: float) -> str:
        """Approve a loan."""
        ran.append(loan_id)
        if loan_id == "LN-ERR":
            raise RuntimeError("core banking timeout for PAN ABCDE1234F")
        return f"approved {loan_id}"

    @tool
    def bureau_pull(loan_id: str) -> dict:
        """Pull the bureau report."""
        return {"loan_id": loan_id, "score": 748}

    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([approve_loan, bureau_pull], wrap_tool_call=guard.wrap, awrap_tool_call=guard.awrap))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile(checkpointer=checkpointer), ran


def _calls(*calls):
    from langchain_core.messages import AIMessage

    return {"messages": [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"call-{i}", "type": "tool_call"} for i, (n, a) in enumerate(calls)])]}


def test_langgraph_allows_records_and_attaches_evidence_per_thread(ledger):
    from warrant.adapters.langgraph import WarrantToolGuard

    w, records = ledger
    graph, ran = _graph(WarrantToolGuard(w, {"approve_loan": APPROVE}))
    config = {"configurable": {"thread_id": "case-1"}}
    graph.invoke(_calls(("bureau_pull", {"loan_id": "LN-1"})), config)
    graph.invoke(_calls(("bureau_pull", {"loan_id": "LN-9"})), {"configurable": {"thread_id": "case-other"}})
    out = graph.invoke(_calls(("approve_loan", GOOD)), config)
    assert ran == ["LN-1"] and out["messages"][-1].content == "approved LN-1"
    (decision,) = records()
    assert decision["decision"] == {"class": "credit.approve", "action": "approve", "subject": "LN-1", "status": "acted"}
    assert decision["mandate"]["clause"] == "4.2" and decision["cost"]["cost_centre"] == "retail-lending"
    assert [e["name"] for e in decision["evidence"]] == ["bureau_pull", "approve_loan.result"]
    assert decision["evidence"][0]["uri"] == "tool://bureau_pull#call-0", "only this thread's lookups are evidence"


def test_langgraph_blocks_deny_and_escalate_without_running_the_tool(ledger):
    from warrant.adapters.langgraph import WarrantToolGuard

    w, records = ledger
    graph, ran = _graph(WarrantToolGuard(w, {"approve_loan": APPROVE}))
    out = graph.invoke(_calls(("approve_loan", WEAK), ("approve_loan", BIG), ("approve_loan", {"amount": 5})))
    weak, big, unreadable = out["messages"][-3:]
    assert ran == []
    assert weak.status == "error" and "policy CR-07 clause 4.1 does not allow it" in weak.content and "Do not retry" in weak.content
    assert big.status == "error" and "requires a human to decide" in big.content
    assert unreadable.status == "error" and "could not read the decision" in unreadable.content
    by_subject = {r["decision"]["subject"]: r for r in records()}
    assert sorted(by_subject) == ["LN-2", "LN-3"], "an unreadable call is blocked and is not a decision"
    assert (by_subject["LN-3"]["decision"]["status"], by_subject["LN-3"]["mandate"]["result"]) == ("withheld", "deny")
    assert by_subject["LN-2"]["mandate"]["result"] == "escalate" and by_subject["LN-2"]["human"] == {"required": True}


def test_langgraph_records_a_failed_tool_without_its_error_text(ledger):
    from warrant.adapters.langgraph import WarrantToolGuard

    w, records = ledger
    graph, ran = _graph(WarrantToolGuard(w, {"approve_loan": APPROVE}))
    with pytest.raises(RuntimeError, match="core banking timeout"):
        graph.invoke(_calls(("approve_loan", {**GOOD, "loan_id": "LN-ERR"})))
    assert ran == ["LN-ERR"]
    (failed,) = records()
    assert (failed["decision"]["subject"], failed["decision"]["status"]) == ("LN-ERR", "failed")
    assert failed["decision"]["summary"] == "ToolCallFailed raised before the action completed" and "ABCDE1234F" not in str(failed)


def test_langgraph_interrupt_pauses_then_records_the_reviewers_verdict(ledger):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from warrant.adapters.langgraph import WarrantToolGuard

    w, records = ledger
    graph, ran = _graph(WarrantToolGuard(w, {"approve_loan": APPROVE}, on_escalate="interrupt"), checkpointer=InMemorySaver())
    approve, refuse = {"configurable": {"thread_id": "t-approve"}}, {"configurable": {"thread_id": "t-refuse"}}

    paused = graph.invoke(_calls(("approve_loan", BIG)), approve)
    (pending,) = paused["__interrupt__"]
    assert pending.value["warrant"] == "escalate" and pending.value["subject"] == "LN-2" and pending.value["clause"] == "4.3"
    assert ran == [] and records() == [], "nothing runs and nothing is recorded while the graph waits for a human"

    done = graph.invoke(Command(resume={"approve": True, "reviewer": "asha@bank.example"}), approve)
    assert ran == ["LN-2"] and done["messages"][-1].content == "approved LN-2"
    graph.invoke(_calls(("approve_loan", {**BIG, "loan_id": "LN-4"})), refuse)
    refused = graph.invoke(Command(resume={"approve": False, "reviewer": "asha@bank.example"}), refuse)
    assert ran == ["LN-2"] and "a human reviewer did not approve it" in refused["messages"][-1].content

    acted, acted_verdict, held, held_verdict = records()
    assert (acted["decision"]["status"], acted["mandate"]["result"], acted["human"]) == ("acted", "escalate", {"required": True, "note": "approved at the graph interrupt"})
    assert acted_verdict["record_type"] == "human_verdict" and acted_verdict["references"]["decision_record_id"] == acted["record_id"]
    assert (acted_verdict["human"]["reviewer"], acted_verdict["human"]["verdict"]) == ("asha@bank.example", "approve")
    assert (held["decision"]["status"], held["decision"]["subject"], held_verdict["human"]["verdict"]) == ("withheld", "LN-4", "reject")
    assert all(r.ok for r in verify_records([acted, acted_verdict, held, held_verdict]))


def test_langgraph_async_wrapper_and_bad_options(ledger):
    from warrant.adapters.langgraph import WarrantToolGuard

    w, records = ledger
    graph, ran = _graph(WarrantToolGuard(w, {"approve_loan": APPROVE}))
    out = asyncio.run(graph.ainvoke(_calls(("approve_loan", GOOD), ("approve_loan", WEAK))))
    assert ran == ["LN-1"] and [m.status for m in out["messages"][-2:]] == ["success", "error"]
    assert sorted(r["decision"]["status"] for r in records()) == ["acted", "withheld"]
    with pytest.raises(ValueError, match="on_escalate"):
        WarrantToolGuard(w, {"approve_loan": APPROVE}, on_escalate="ask")
    with pytest.raises(ValueError, match="decisions is empty"):
        WarrantToolGuard(w, {})


# -- Claude Agent SDK -----------------------------------------------------------

TOOL = "mcp__bank__approve_loan"


def _hook_input(event, tool_name, tool_input, call_id, **extra):
    return {"hook_event_name": event, "session_id": "s-1", "transcript_path": "/tmp/t", "cwd": "/tmp", "tool_name": tool_name,
            "tool_input": tool_input, "tool_use_id": call_id, **extra}


def _claude(w, **options):
    pytest.importorskip("claude_agent_sdk")
    from warrant.adapters.claude_agent import WarrantHooks

    guard = WarrantHooks(w, {TOOL: APPROVE}, **options)
    hooks = guard.hooks()
    run = lambda event, data: asyncio.run(hooks[event][-1].hooks[0](data, data.get("tool_use_id"), {"signal": None}))  # noqa: E731
    return guard, run


def test_claude_hooks_are_well_formed_and_keep_existing_hooks(ledger):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from warrant.adapters.claude_agent import WarrantHooks

    async def mine(data, tool_use_id, context):
        return {}

    hooks = WarrantHooks(ledger[0], {TOOL: APPROVE}).hooks({"PreToolUse": [HookMatcher(matcher="Bash", hooks=[mine])]})
    assert sorted(hooks) == ["PostToolUse", "PostToolUseFailure", "PreToolUse", "Stop"]
    assert [m.matcher for m in hooks["PreToolUse"]] == ["Bash", None] and hooks["PreToolUse"][0].hooks == [mine]
    assert ClaudeAgentOptions(hooks=hooks).hooks is hooks


def test_claude_allow_passes_through_and_records_after_the_tool_runs(ledger):
    from claude_agent_sdk import AssistantMessage, TextBlock

    w, records = ledger
    guard, run = _claude(w, pricer=lambda provider, model, tin, tout: (tin * 0.25 + tout * 1.25) / 1000)
    assert run("PreToolUse", _hook_input("PreToolUse", "Read", {"file_path": "/etc/passwd"}, "c0")) == {}
    assert run("PreToolUse", _hook_input("PreToolUse", TOOL, GOOD, "c2")) == {}, "allow never overrides the host's own permission flow"
    assert records() == [], "nothing is recorded until the tool has run"

    guard.observe(AssistantMessage(content=[TextBlock(text="checking")], model="claude-opus-5", usage={"input_tokens": 1200, "output_tokens": 300}, session_id="s-1"))
    run("PostToolUse", _hook_input("PostToolUse", "Read", {"file_path": "x"}, "c0", tool_response="file text"))
    run("PostToolUse", _hook_input("PostToolUse", "mcp__bank__bureau_pull", {"loan_id": "LN-1"}, "c1", tool_response={"score": 748}))
    run("PostToolUse", _hook_input("PostToolUse", TOOL, GOOD, "c2", tool_response={"ok": True}))
    (decision,) = records()
    assert decision["decision"] == {"class": "credit.approve", "action": "approve", "subject": "LN-1", "status": "acted"}
    assert [e["name"] for e in decision["evidence"]] == ["mcp__bank__bureau_pull", f"{TOOL}.result", "anthropic/claude-opus-5"], "host file tools are not evidence by default"
    assert decision["cost"]["amount"] == 0.675 and decision["cost"]["breakdown"][0]["tokens_in"] == 1200


def test_claude_deny_and_escalate_block_with_a_reason_and_record_withheld(ledger):
    w, records = ledger
    _, run = _claude(w)
    denied = run("PreToolUse", _hook_input("PreToolUse", TOOL, WEAK, "c1"))["hookSpecificOutput"]
    escalated = run("PreToolUse", _hook_input("PreToolUse", TOOL, BIG, "c2"))["hookSpecificOutput"]
    unreadable = run("PreToolUse", _hook_input("PreToolUse", TOOL, {"amount": 5}, "c3"))["hookSpecificOutput"]
    assert denied == {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": denied["permissionDecisionReason"]}
    assert "policy CR-07 clause 4.1 does not allow it" in denied["permissionDecisionReason"]
    assert escalated["permissionDecision"] == "deny" and "requires a human to decide" in escalated["permissionDecisionReason"]
    assert unreadable["permissionDecision"] == "deny" and "could not read the decision" in unreadable["permissionDecisionReason"]
    assert [(r["decision"]["subject"], r["decision"]["status"], r["mandate"]["result"]) for r in records()] == [("LN-3", "withheld", "deny"), ("LN-2", "withheld", "escalate")]


def test_claude_ask_defers_to_the_host_and_records_either_way(ledger):
    w, records = ledger
    _, run = _claude(w, on_escalate="ask")
    for call_id, loan in (("c1", "LN-A"), ("c2", "LN-B")):
        out = run("PreToolUse", _hook_input("PreToolUse", TOOL, {**BIG, "loan_id": loan}, call_id))
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert records() == []
    run("PostToolUse", _hook_input("PostToolUse", TOOL, {**BIG, "loan_id": "LN-A"}, "c1", tool_response="done"))   # the human said yes
    run("Stop", {"hook_event_name": "Stop", "session_id": "s-1", "transcript_path": "/tmp/t", "cwd": "/tmp", "stop_hook_active": False})  # and never approved c2
    approved, never = records()
    assert (approved["decision"]["subject"], approved["decision"]["status"], approved["human"]["note"]) == ("LN-A", "acted", "approved in the host's permission prompt")
    assert (never["decision"]["subject"], never["decision"]["status"], never["human"]["note"]) == ("LN-B", "withheld", "not approved in the host's permission prompt")


def test_claude_a_failed_tool_is_a_failed_decision_without_the_error_text(ledger):
    w, records = ledger
    _, run = _claude(w)
    run("PostToolUseFailure", _hook_input("PostToolUseFailure", TOOL, GOOD, "c1", error="core banking timeout for PAN ABCDE1234F"))
    run("PostToolUseFailure", _hook_input("PostToolUseFailure", "Bash", {"command": "ls"}, "c2", error="boom"))
    (failed,) = records()
    assert failed["decision"]["status"] == "failed" and failed["decision"]["summary"] == "ToolCallFailed raised before the action completed"
    assert "ABCDE1234F" not in str(failed)
    with pytest.raises(ValueError, match="on_escalate"):
        _claude(w, on_escalate="interrupt")
