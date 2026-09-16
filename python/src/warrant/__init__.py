"""Warrant: the decision ledger for AI agents.

    from warrant import Warrant, AgentInfo

    w = Warrant(stream="lending", agent=AgentInfo("credit-underwriter", "2.3.1"))
    with w.decide("credit.approve", subject="LN-20431") as d:
        verdict = d.check(amount=450000, bureau_score=748)
        d.evidence("bureau_pull", uri="cibil://req/88213", content=bureau_json)
        d.act("approve", summary="...")
    w.outcome(subject="LN-20431", label="performing")
"""

from warrant.client import AgentInfo, Decision, PolicyEngine, Unreplayable, Verdict, Warrant, current_decision
from warrant.redaction import Redactor
from warrant.schema import SCHEMA_VERSION, ValidationError, load_schema, validate
from warrant.store import SQLiteStore
from warrant.verify import StreamReport, verify_records

__version__ = "0.1.0"

__all__ = [
    "AgentInfo",
    "Decision",
    "PolicyEngine",
    "Redactor",
    "SCHEMA_VERSION",
    "SQLiteStore",
    "StreamReport",
    "Unreplayable",
    "ValidationError",
    "Verdict",
    "Warrant",
    "__version__",
    "current_decision",
    "load_schema",
    "validate",
    "verify_records",
]
