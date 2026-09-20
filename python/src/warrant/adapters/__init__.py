"""Adapters: gate and record decisions without touching the code that makes them.

For an agent framework, most tool calls are not decisions. You name the tools that are, and how to
read a decision out of each call::

    from warrant.adapters import ToolDecision

    decisions = {"approve_loan": ToolDecision("credit.approve", subject="loan_id", action="approve")}

``warrant.adapters.claude_agent``, ``warrant.adapters.langgraph`` and ``warrant.adapters.temporal``
take that mapping.

A model that answers typed questions crosses a different boundary: :class:`DecisionModel` in
``warrant.adapters.base``, with ``warrant.adapters.jev`` as its first implementation. Nothing above
that line knows which vendor answered, which is what keeps a vendor's vocabulary out of the
decision record schema and lets one be replaced without touching the ledger.
"""

from warrant.adapters._core import AdapterError, Pricer, ToolDecision
from warrant.adapters.base import DecisionModel, ModelAnswer, ModelError, ModelResult

__all__ = [
    "AdapterError",
    "DecisionModel",
    "ModelAnswer",
    "ModelError",
    "ModelResult",
    "Pricer",
    "ToolDecision",
]
