"""JSON schema for an assess reply: one verdict per component."""

from __future__ import annotations

from typing import Any

import jsonschema

from ..components import COMPONENT_IDS

_COMPONENT_VERDICT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_clarification"],
    "properties": {
        "needs_clarification": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}

ASSESS_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "clin-genomic-analysis-benchmark assess reply",
    "type": "object",
    "additionalProperties": False,
    "required": list(COMPONENT_IDS),
    "properties": {cid: _COMPONENT_VERDICT for cid in COMPONENT_IDS},
}


def validate_assess_result(payload: object) -> list[str]:
    """Return schema errors for an assess reply (empty if valid)."""
    validator = jsonschema.Draft202012Validator(ASSESS_RESULT_SCHEMA)
    return [error.message for error in validator.iter_errors(payload)]
