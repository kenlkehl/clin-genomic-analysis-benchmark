"""JSON-schema contract for question.json (in) and result.json (out)."""

from __future__ import annotations

from typing import Any, Literal

import jsonschema

from ..components import COMPONENT_IDS
from ..gold_standard.answer_types import validate_answer

Stage = Literal["assess", "analyze"]
STAGES: tuple[Stage, ...] = ("assess", "analyze")


# ---- input (question.json) ----

QUESTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "clin-genomic-analysis-benchmark question",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version", "question_id", "question_text", "cohort", "stage",
        "cohort_dir", "data_dictionary_path", "scratch_dir",
        "instructions", "assessment_components",
    ],
    "properties": {
        "contract_version": {"const": "4"},
        "question_id": {"type": "string"},
        "question_text": {"type": "string"},
        "cohort": {"type": "string"},
        "stage": {"enum": list(STAGES)},
        "cohort_dir": {"type": "string"},
        "data_dictionary_path": {"type": "string"},
        "scratch_dir": {"type": "string"},
        "instructions": {"type": "string"},
        "assessment_components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "description"],
                "properties": {
                    "id": {"enum": list(COMPONENT_IDS)},
                    "label": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
            },
            "minItems": len(COMPONENT_IDS),
            "maxItems": len(COMPONENT_IDS),
        },
        # Optional: for the `analyze` stage, agents may want to know what they
        # concluded in assess (defensive only — they can also store this
        # themselves between stages).
        "prior_classification": {"enum": ["ambiguous", "unambiguous"]},
        # Optional: budget hints
        "max_runtime_seconds": {"type": "integer", "minimum": 1},
    },
}


# ---- output (result.json) ----

_COMPONENT_VERDICT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["needs_clarification"],
    "properties": {
        "needs_clarification": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}

_ASSESS_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(COMPONENT_IDS),
    "properties": {cid: _COMPONENT_VERDICT for cid in COMPONENT_IDS},
}

_ANALYZE_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer_type", "answer"],
    "properties": {
        "answer_type": {
            "type": "string",
            "enum": [
                "count", "proportion", "median_with_ci",
                "hazard_ratio_with_ci", "odds_ratio_with_ci",
                "pvalue", "categorical", "categorical_distribution",
            ],
        },
        "answer": {"type": "object"},
        "methods": {"type": "string"},
        "supporting_evidence": {"type": "object"},
    },
}


_RESULT_SCHEMAS: dict[str, dict] = {
    "assess": _ASSESS_RESULT,
    "analyze": _ANALYZE_RESULT,
}


def validate_question(payload: dict) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    validator = jsonschema.Draft202012Validator(QUESTION_SCHEMA)
    errors = [e.message for e in validator.iter_errors(payload)]
    components = payload.get("assessment_components")
    if isinstance(components, list):
        ids = [item.get("id") for item in components if isinstance(item, dict)]
        if ids != list(COMPONENT_IDS):
            errors.append("assessment_components must list the canonical components in order")
    return errors


def validate_result(payload: dict, stage: Stage) -> list[str]:
    schema = _RESULT_SCHEMAS[stage]
    validator = jsonschema.Draft202012Validator(schema)
    errors = [e.message for e in validator.iter_errors(payload)]
    if stage == "analyze" and not errors:
        errors.extend(validate_answer(payload["answer_type"], payload["answer"]))
    return errors
