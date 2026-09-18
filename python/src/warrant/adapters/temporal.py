"""Temporal adapter: a worker interceptor that gates and records the activities you name as decisions.

    from temporalio.worker import Worker
    from warrant.adapters import ToolDecision
    from warrant.adapters.temporal import WarrantInterceptor

    guard = WarrantInterceptor(w, {"disburse": ToolDecision("credit.disburse", subject="loan_id",
                                                            inputs=["amount", "bureau_score", "foir"])})
    worker = Worker(client, task_queue=..., workflows=[...], activities=[...], interceptors=[guard])

Workflow code does not change. Before a mapped activity runs, its arguments are checked
against the policy. ``deny`` and ``escalate`` stop it: the attempt is recorded as withheld and
the activity fails with a non-retryable ``ApplicationError`` of type ``WarrantDenied`` or
``WarrantEscalated``, which the workflow can catch and route to a person. ``allow`` and
``unchecked`` change nothing. After the activity returns, the decision is recorded with the
run's other activity results attached as evidence, by hash, and with the Temporal identity
(namespace, workflow, run, activity, attempt) as evidence, so an auditor can open the run.
An activity that raises is recorded as a failed decision, without its error text.

One record per attempt, with a record id derived from the attempt's Temporal identity, so a
re-sent batch is absorbed by the store's duplicate check and a retry is a new record.
The policy check is in-process and recording is asynchronous: a Warrant outage never
touches an activity. Requires ``pip install "warrantai[temporal]"``.
"""

from __future__ import annotations

import contextvars
import dataclasses
import inspect
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from warrant.adapters._core import AdapterError, EvidenceItem, EvidenceLog, ModelUsage, Pricer, ToolDecision, blocked_message, evaluate, record
from warrant.client import Warrant
from warrant.hashing import content_hash
from warrant.ids import deterministic_ulid

try:
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor
except ImportError as exc:  # pragma: no cover
    raise ImportError('the Temporal adapter needs temporalio: pip install "warrantai[temporal]"') from exc

log = logging.getLogger("warrant.adapters.temporal")

DENIED = "WarrantDenied"
ESCALATED = "WarrantEscalated"
UNREADABLE = "WarrantUnreadable"

_usage: contextvars.ContextVar[Optional[List[ModelUsage]]] = contextvars.ContextVar("warrant_temporal_usage", default=None)


def model_usage(provider: str, model: str, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Report a model call made inside an activity, so its cost lands on that activity's decision.

    Ignored inside an activity that is not mapped as a decision. Raises outside an activity.
    """
    if not activity.in_activity():
        raise RuntimeError("model_usage() must be called from inside a Temporal activity")
    items = _usage.get()
    if items is not None:
        items.append(ModelUsage(provider, model, int(tokens_in), int(tokens_out)))


def _every_activity(activity_type: str) -> bool:
    return True


class WarrantInterceptor(Interceptor):
    """Gates and records the activities named in ``decisions``; collects the others as evidence."""

    def __init__(
        self,
        client: Warrant,
        decisions: Mapping[str, ToolDecision],
        *,
        evidence: Callable[[str], bool] = _every_activity,
        pricer: Optional[Pricer] = None,
        max_runs: int = 1000,
    ) -> None:
        if not decisions:
            raise ValueError("decisions is empty: name at least one activity type whose runs are decisions")
        if max_runs < 1:
            raise ValueError("max_runs must be at least 1")
        self._client = client
        self._decisions = dict(decisions)
        self._is_evidence = evidence
        self._pricer = pricer
        # Evidence is kept per workflow run; a run can end without a decision and nothing tells
        # the worker, so the log forgets the oldest runs past max_runs.
        self._evidence = EvidenceLog(max_sessions=max_runs)

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _Inbound(next, self)

    # -- one activity attempt ------------------------------------------------

    async def _execute(self, next: ActivityInboundInterceptor, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        run_key = f"{info.workflow_id}/{info.workflow_run_id}"
        mapping = self._decisions.get(info.activity_type)
        if mapping is None:
            result = await next.execute_activity(input)
            if self._is_evidence(info.activity_type):
                self._evidence.add(run_key, info.activity_type, info.activity_id, result)
            return result

        try:
            subject, inputs = mapping.read(info.activity_type, _arguments(input.fn, input.args))
        except AdapterError as exc:
            log.warning("warrant: %s blocked, not a decision: %s", info.activity_type, exc)
            raise ApplicationError(f"Warrant could not read the decision from this call: {exc}", type=UNREADABLE, non_retryable=True) from None

        record_id = deterministic_ulid(
            int(info.current_attempt_scheduled_time.timestamp() * 1000),
            f"{info.namespace}|{info.workflow_id}|{info.workflow_run_id}|{info.activity_id}|{info.attempt}",
        )
        identity = _identity(info)
        verdict = evaluate(self._client, mapping.decision_class, inputs)
        if verdict.result in ("deny", "escalate"):
            note = "awaiting a human's decision; the activity was not run" if verdict.result == "escalate" else None
            self._record(mapping, info, subject, inputs, status="withheld", record_id=record_id, identity=identity, evidence=self._evidence.take(run_key), human_note=note)
            raise ApplicationError(
                blocked_message(info.activity_type, verdict),
                {"record_id": record_id, "decision_class": mapping.decision_class, "subject": subject, "result": verdict.result,
                 "policy_id": verdict.policy_id, "clause": verdict.clause, "reason": verdict.reason},
                type=ESCALATED if verdict.result == "escalate" else DENIED,
                non_retryable=True,
            )

        usage: List[ModelUsage] = []
        token = _usage.set(usage)
        try:
            result = await next.execute_activity(input)
        except BaseException:
            # A failed attempt keeps the run's evidence for the next attempt. The error text is not recorded: it can carry customer data.
            self._record(mapping, info, subject, inputs, status="failed", record_id=record_id, identity=identity, evidence=self._evidence.peek(run_key), usage=usage)
            raise
        finally:
            _usage.reset(token)
        self._record(mapping, info, subject, inputs, status="acted", record_id=record_id, identity=identity, evidence=self._evidence.take(run_key), result=result, usage=usage)
        return result

    def _record(
        self,
        mapping: ToolDecision,
        info: "activity.Info",
        subject: str,
        inputs: Mapping[str, Any],
        *,
        status: str,
        record_id: str,
        identity: EvidenceItem,
        evidence: Sequence[EvidenceItem],
        result: Any = None,
        usage: Sequence[ModelUsage] = (),
        human_note: Optional[str] = None,
    ) -> None:
        record(
            self._client, mapping, info.activity_type, subject, inputs,
            status=status, evidence=[identity, *evidence], result=result, usage=usage, pricer=self._pricer,
            human_note=human_note, record_id=record_id,
        )


class _Inbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, owner: WarrantInterceptor) -> None:
        super().__init__(next)
        self._owner = owner

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        return await self._owner._execute(self.next, input)


def _arguments(fn: Callable[..., Any], args: Sequence[Any]) -> Dict[str, Any]:
    """Activity arguments by parameter name; a single dataclass argument is read field by field."""
    try:
        bound = inspect.signature(fn).bind(*args)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"activity arguments do not match its signature: {type(exc).__name__}") from exc
    values = dict(bound.arguments)
    if len(values) == 1:
        (only,) = values.values()
        if dataclasses.is_dataclass(only) and not isinstance(only, type):
            return {f.name: getattr(only, f.name) for f in dataclasses.fields(only)}
    return values


def _identity(info: "activity.Info") -> EvidenceItem:
    """The Temporal execution as evidence: the ids in the URI, the full identity behind the hash."""
    fields = {
        "namespace": info.namespace, "workflow_type": info.workflow_type, "workflow_id": info.workflow_id,
        "workflow_run_id": info.workflow_run_id, "activity_type": info.activity_type, "activity_id": info.activity_id,
        "attempt": info.attempt, "task_queue": info.task_queue,
    }
    uri = (f"temporal://{info.namespace}/{info.workflow_id}/{info.workflow_run_id}/activity/{info.activity_id}"
           f"?attempt={info.attempt}&type={info.activity_type}&queue={info.task_queue}")
    return EvidenceItem("temporal.execution", uri, content_hash(fields), type="other")
