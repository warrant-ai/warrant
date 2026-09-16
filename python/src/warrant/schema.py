"""Load and validate Warrant decision records against the published schema."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Dict, List

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as _JsonSchemaError

SCHEMA_VERSION = "0"
_SCHEMA_FILE = f"decision-record.v{SCHEMA_VERSION}.json"


class ValidationError(ValueError):
    """A record does not conform to the decision record schema.

    ``errors`` holds one human-readable message per violation, each prefixed
    with the JSON path of the offending field.
    """

    def __init__(self, errors: List[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def load_schema() -> Dict[str, Any]:
    """Return the decision record JSON Schema shipped with this package."""
    text = resources.files("warrant").joinpath("schema", _SCHEMA_FILE).read_text(encoding="utf-8")
    return json.loads(text)


_validator = Draft202012Validator(load_schema())


def _describe(error: _JsonSchemaError) -> str:
    path = "/".join(str(p) for p in error.absolute_path) or "(root)"
    return f"{path}: {error.message}"


def validate(record: Any) -> None:
    """Raise ``ValidationError`` listing every violation, or return ``None`` if valid.

    ``record`` is expected to be the parsed JSON object, not a string.
    """
    if not isinstance(record, dict):
        raise ValidationError([f"(root): expected an object, got {type(record).__name__}"])
    errors = sorted(_validator.iter_errors(record), key=lambda e: list(e.absolute_path))
    if errors:
        raise ValidationError([_describe(e) for e in errors])
