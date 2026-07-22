"""JSON-schema contract for question.json (in) and result.json (out)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import jsonschema

Stage = Literal["classify", "disambiguate", "analyze"]


# ---- input (question.json) ----

QUESTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "clin-genomic-analysis-benchmark question",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "question_id", "question_text", "cohort", "category", "stage",
        "cohort_dir", "data_dictionary_path", "scratch_dir", "instructions",
    ],
    "properties": {
        "question_id": {"type": "string"},
        "question_text": {"type": "string"},
        "cohort": {"type": "string"},
        "category": {"type": "integer", "minimum": 1, "maximum": 8},
        "stage": {"enum": ["classify", "disambiguate", "analyze"]},
        "cohort_dir": {"type": "string"},
        "data_dictionary_path": {"type": "string"},
        "scratch_dir": {"type": "string"},
        "instructions": {"type": "string"},
        # Optional: for `disambiguate` and `analyze` stages, agents may want
        # to know what they classified the question as (defensive only — they can
        # also store this themselves between stages).
        "prior_classification": {"enum": ["ambiguous", "unambiguous"]},
        # Optional: budget hints
        "max_runtime_seconds": {"type": "integer", "minimum": 1},
    },
}


# ---- output (result.json) ----

_CLASSIFY_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["classification"],
    "properties": {
        "classification": {"enum": ["ambiguous", "unambiguous"]},
        "rationale": {"type": "string"},
    },
}

_DISAMBIGUATE_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["concepts"],
    "properties": {
        "concepts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
    },
}

_ANALYZE_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["answer_type", "answer"],
    "properties": {
        "answer_type": {
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
    "classify": _CLASSIFY_RESULT,
    "disambiguate": _DISAMBIGUATE_RESULT,
    "analyze": _ANALYZE_RESULT,
}


def validate_question(payload: dict) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    validator = jsonschema.Draft202012Validator(QUESTION_SCHEMA)
    return [e.message for e in validator.iter_errors(payload)]


def validate_result(payload: dict, stage: Stage) -> list[str]:
    schema = _RESULT_SCHEMAS[stage]
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(payload)]
