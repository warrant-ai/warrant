"""Framework adapters: gate and record an agent framework's tool calls without touching the agent's code.

Most tool calls are not decisions. You name the tools that are, and how to read a decision
out of each call::

    from warrant.adapters import ToolDecision

    decisions = {"approve_loan": ToolDecision("credit.approve", subject="loan_id", action="approve")}

``warrant.adapters.claude_agent`` and ``warrant.adapters.langgraph`` take that mapping.
"""

from warrant.adapters._core import AdapterError, Pricer, ToolDecision

__all__ = ["AdapterError", "Pricer", "ToolDecision"]
