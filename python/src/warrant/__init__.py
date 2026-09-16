"""Warrant: the decision ledger for AI agents.

This release ships the decision record schema v0 and a validator. The SDK's
``decide()`` context manager, the policy check, the local store and replay
arrive in the following releases; see https://warrantai.dev.
"""

from warrant.schema import SCHEMA_VERSION, ValidationError, load_schema, validate

__version__ = "0.0.1"

__all__ = ["SCHEMA_VERSION", "ValidationError", "__version__", "load_schema", "validate"]
