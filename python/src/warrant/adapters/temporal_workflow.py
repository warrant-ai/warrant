"""Workflow-side helpers for the Temporal adapter: explicit decisions and approvals from workflow code.

Import this module inside ``workflow.unsafe.imports_passed_through()``. The worker's interceptor
must see the same module object as the workflow code, and the SDK it belongs to is not written
to be re-imported under the workflow sandbox.

    with workflow.unsafe.imports_passed_through():
        from warrant.adapters import temporal_workflow as warrant

    @workflow.defn
    class LoanApproval:
        @workflow.run
        async def run(self, loan):
            if workflow.patched("warrant-underwrite-decision"):           # new call sites go behind a patch
                verdict = await warrant.decide("credit.approve", subject=loan.id, inputs={...},
                                               action="approve", summary=result.reason)
            try:
                return await workflow.execute_activity(disburse, loan, start_to_close_timeout=...)
            except ActivityError as exc:
                escalated = warrant.escalation(exc)
                if escalated is None:
                    raise
                await workflow.wait_condition(lambda: self.review is not None, timeout=timedelta(days=2))
                if self.review.approve:
                    return await warrant.approved(disburse, loan, reviewer=self.review.reviewer,
                                                  record_id=escalated["record_id"], start_to_close_timeout=...)
                await warrant.rejected(escalated["record_id"], reviewer=self.review.reviewer, note=self.review.reason)

Everything here is deterministic: ids come from ``workflow.uuid4()`` and ``workflow.now()``, and
records are written by the ``warrant.record`` local activity, whose result Temporal keeps in
history so replay never writes twice. Adding a ``decide()`` call to a running workflow shifts the
commands that follow it, so put new call sites behind ``workflow.patched``.
"""

from __future__ import annotations

import contextvars
from datetime import timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import StartActivityInput, WorkflowInboundInterceptor, WorkflowOutboundInterceptor

from warrant.ids import ULID_RE, deterministic_ulid

RECORD_ACTIVITY = "warrant.record"
"""Name of the local activity that writes workflow-side records; register ``WarrantInterceptor.record_activity``."""
APPROVAL_HEADER = "warrant-approval"
DENIED = "WarrantDenied"
ESCALATED = "WarrantEscalated"
UNREADABLE = "WarrantUnreadable"

_RECORD_TIMEOUT = timedelta(seconds=30)
_RECORD_RETRY = RetryPolicy(maximum_attempts=3)
_pending: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("warrant_pending_approval", default=None)
_unset = object()


def blocked(exc: BaseException) -> Optional[Dict[str, Any]]:
    """The details of a decision the gate blocked (``WarrantDenied`` or ``WarrantEscalated``), else ``None``.

    The details carry ``record_id``, ``result``, ``decision_class``, ``subject``, ``policy_id``, ``clause``, ``reason``.
    """
    cause = exc.cause if isinstance(exc, ActivityError) else exc
    if isinstance(cause, ApplicationError) and cause.type in (DENIED, ESCALATED) and cause.details:
        detail = cause.details[0]
        return dict(detail) if isinstance(detail, Mapping) else None
    return None


def escalation(exc: BaseException) -> Optional[Dict[str, Any]]:
    """The details of an escalated decision, else ``None``. A denial is not an escalation: nobody can approve it."""
    detail = blocked(exc)
    return detail if detail is not None and detail.get("result") == "escalate" else None


async def decide(
    decision_class: str,
    *,
    subject: str,
    inputs: Mapping[str, Any],
    action: str,
    summary: Optional[str] = None,
    alternatives: Optional[Sequence[str]] = None,
    on_behalf_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a decision the workflow itself makes, and return the mandate's verdict to branch on.

    Allowed (or unchecked) decisions are recorded as acted with ``action``; denied and escalated
    ones as withheld. The returned dict has ``result``, ``policy_id``, ``policy_version``,
    ``clause``, ``reason``, ``flagged``, ``status`` and ``record_id``.
    """
    if not isinstance(subject, str) or not subject:
        raise ValueError("subject must be a non-empty string")
    if not isinstance(action, str) or not action:
        raise ValueError("action must be a non-empty string")
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a mapping of policy input names to values")
    request = {
        "kind": "decision",
        "record_id": _record_id("wf"),
        "decision_class": decision_class,
        "subject": subject,
        "inputs": dict(inputs),
        "action": action,
        "summary": summary,
        "alternatives": list(alternatives) if alternatives else None,
        "on_behalf_of": on_behalf_of,
        "identity": _identity(),
    }
    return await workflow.execute_local_activity(RECORD_ACTIVITY, request, start_to_close_timeout=_RECORD_TIMEOUT, retry_policy=_RECORD_RETRY)


async def approved(activity: Any, arg: Any = _unset, *, args: Sequence[Any] = (), reviewer: str, record_id: str, note: Optional[str] = None, **options: Any) -> Any:
    """Run an escalated activity now that a person has approved it.

    ``reviewer`` is who approved, from your own Update or Signal payload; ``record_id`` is the
    escalated decision's, from ``escalation(exc)``. The reviewer's verdict is appended to the
    ledger as its own sealed record before the activity runs, and the activity's record names the
    reviewer. ``options`` are ``workflow.start_activity`` keyword arguments (timeouts, retry policy).
    """
    _require_text(reviewer, "reviewer")
    if not isinstance(record_id, str) or not ULID_RE.match(record_id):
        raise ValueError("record_id must be the escalated decision's record id")
    approval = {"record_id": record_id, "reviewer": reviewer, "note": note}
    token = _pending.set(approval)
    try:
        handle = workflow.start_activity(activity, args=args, **options) if arg is _unset else workflow.start_activity(activity, arg, args=args, **options)
        attached = _pending.get() is None
    finally:
        _pending.reset(token)
    if not attached:
        handle.cancel()
        raise RuntimeError(
            "the approval was not attached to the activity: import warrant.adapters.temporal_workflow inside "
            "workflow.unsafe.imports_passed_through() and add the WarrantInterceptor to the worker's interceptors"
        )
    return await handle


async def rejected(record_id: str, *, reviewer: str, note: Optional[str] = None) -> Dict[str, Any]:
    """Record that a person rejected an escalated decision, or that the wait for one timed out."""
    _require_text(reviewer, "reviewer")
    if not isinstance(record_id, str) or not ULID_RE.match(record_id):
        raise ValueError("record_id must be the escalated decision's record id")
    request = {
        "kind": "verdict",
        "record_id": _record_id("verdict"),
        "decision_record_id": record_id,
        "reviewer": reviewer,
        "verdict": "reject",
        "note": note,
    }
    return await workflow.execute_local_activity(RECORD_ACTIVITY, request, start_to_close_timeout=_RECORD_TIMEOUT, retry_policy=_RECORD_RETRY)


class WarrantWorkflowInbound(WorkflowInboundInterceptor):
    """Installed by ``WarrantInterceptor``; carries an approval from ``approved()`` to the activity as a header."""

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_Outbound(outbound))


class _Outbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> Any:
        approval = _pending.get()
        if approval is not None:
            _pending.set(None)
            input.headers = {**input.headers, APPROVAL_HEADER: workflow.payload_converter().to_payload(approval)}
        return self.next.start_activity(input)


def _record_id(tag: str) -> str:
    info = workflow.info()
    ts_ms = int(workflow.now().timestamp() * 1000)
    return deterministic_ulid(ts_ms, f"{info.namespace}|{info.workflow_id}|{info.run_id}|{tag}|{workflow.uuid4()}")


def _identity() -> Dict[str, Any]:
    info = workflow.info()
    return {
        "namespace": info.namespace, "workflow_type": info.workflow_type, "workflow_id": info.workflow_id,
        "workflow_run_id": info.run_id, "first_execution_run_id": info.first_execution_run_id,
        "task_queue": info.task_queue, "attempt": info.attempt,
    }


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
