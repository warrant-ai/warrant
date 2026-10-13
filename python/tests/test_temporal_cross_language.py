"""A JS workflow approves an escalated activity that a Python worker runs.

The `warrant-approval` header is written by the JS workflow-side helper `approved()` and read by
the Python activity interceptor, so this pins the header's name and contents across the two SDKs.
Needs Node and `npm ci` in js/; skipped otherwise (CI runs it in its own job).
"""

import asyncio
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

pytest.importorskip("celpy")
pytest.importorskip("temporalio")

from warrant import verify_records  # noqa: E402
from warrant.adapters.temporal import WarrantInterceptor  # noqa: E402
from warrant.hashing import content_hash  # noqa: E402
from warrant.ids import deterministic_ulid  # noqa: E402

from test_adapter_temporal import BIG, DISBURSE, ledger  # noqa: E402,F401  (ledger is a fixture)

JS = Path(__file__).resolve().parents[2] / "js"
WORKER = JS / "test" / "cross-language" / "temporal-worker.mjs"
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (JS / "node_modules" / "@temporalio" / "worker").exists(),
    reason="needs node and `npm ci` in js/",
)


def _wait_ready(node: "subprocess.Popen[str]") -> None:
    """Read the worker's output until it says ready (the SDK's core may log first), then keep draining it so the pipe never fills."""
    seen = []
    for line in node.stdout:
        if line.strip() == "ready":
            threading.Thread(target=lambda: [None for _ in node.stdout], daemon=True).start()
            return
        seen.append(line)
    raise AssertionError("the JS worker exited before it was ready:\n" + "".join(seen))


def test_a_js_workflow_approves_an_escalated_python_activity(ledger):  # noqa: F811
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from temporal_app import disburse_loan

    w, records = ledger
    escalated = deterministic_ulid(1758000000000, "cross-language|escalated")

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            address = env.client.service_client.config.target_host
            node = subprocess.Popen(["node", str(WORKER), address, env.client.namespace, "xl-workflows"], cwd=JS,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                await asyncio.wait_for(asyncio.to_thread(_wait_ready, node), 120)
                async with Worker(env.client, task_queue="xl-activities", activities=[disburse_loan],
                                  interceptors=[WarrantInterceptor(w, {"disburse_loan": DISBURSE})]):
                    return await env.client.execute_workflow(
                        "crossLanguageApproval", args=[BIG, escalated, "asha@bank.example", "DSCR checked", "xl-activities"],
                        id=f"xl-{uuid.uuid4()}", task_queue="xl-workflows")
            finally:
                node.terminate()
                node.wait(30)

    assert asyncio.run(go()) == "disbursed LN-2"
    verdict, acted = records()
    assert verdict["record_type"] == "human_verdict" and verdict["references"] == {"decision_record_id": escalated}
    assert (verdict["human"]["reviewer"], verdict["human"]["verdict"], verdict["human"]["note"]) == ("asha@bank.example", "approve", "DSCR checked")
    assert acted["decision"]["status"] == "acted" and acted["mandate"]["result"] == "escalate"
    assert acted["human"] == {"required": True, "reviewer": "asha@bank.example", "note": f"approved after escalation {escalated}"}
    escalation = next(e for e in acted["evidence"] if e["name"] == "warrant.escalation")
    assert escalation["uri"] == f"warrant://record/{escalated}" and escalation["content_hash"] == content_hash(escalated)
    assert all(r.ok for r in verify_records([verdict, acted]))
