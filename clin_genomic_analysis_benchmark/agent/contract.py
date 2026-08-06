"""JSON-schema contract for question.json (in) and result.json (out)."""

from __future__ import annotations

from typing import Any, Literal

import jsonschema

from ..concepts import CONCEPT_IDS
from ..gold_standard.answer_types import validate_answer

Stage = Literal["classify", "disambiguate", "analyze"]


# ---- input (question.json) ----

QUESTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "clin-genomic-analysis-benchmark question",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version", "question_id", "question_text", "cohort", "category",
        "stage", "cohort_dir", "data_dictionary_path", "scratch_dir",
        "instructions", "disambiguation_concept_menu",
    ],
    "properties": {
        "contract_version": {"const": "2"},
        "question_id": {"type": "string"},
        "question_text": {"type": "string"},
        "cohort": {"type": "string"},
        "category": {"type": "integer", "minimum": 1, "maximum": 8},
        "stage": {"enum": ["classify", "disambiguate", "analyze"]},
        "cohort_dir": {"type": "string"},
        "data_dictionary_path": {"type": "string"},
        "scratch_dir": {"type": "string"},
        "instructions": {"type": "string"},
        "disambiguation_concept_menu": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "description"],
                "properties": {
                    "id": {"enum": list(CONCEPT_IDS)},
                    "label": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
            },
            "minItems": len(CONCEPT_IDS),
            "maxItems": len(CONCEPT_IDS),
        },
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
    "additionalProperties": False,
    "required": ["classification"],
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["ambiguous", "unambiguous"],
        },
        "rationale": {"type": "string"},
    },
}

_DISAMBIGUATE_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["concept_ids"],
    "properties": {
        "concept_ids": {
            "type": "array",
            "items": {"type": "string", "enum": list(CONCEPT_IDS)},
            "minItems": 1,
            "uniqueItems": True,
        },
    },
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
    "classify": _CLASSIFY_RESULT,
    "disambiguate": _DISAMBIGUATE_RESULT,
    "analyze": _ANALYZE_RESULT,
}


def validate_question(payload: dict) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    validator = jsonschema.Draft202012Validator(QUESTION_SCHEMA)
    errors = [e.message for e in validator.iter_errors(payload)]
    menu = payload.get("disambiguation_concept_menu")
    if isinstance(menu, list):
        menu_ids = [item.get("id") for item in menu if isinstance(item, dict)]
        if menu_ids != list(CONCEPT_IDS):
            errors.append("disambiguation_concept_menu must contain the canonical menu in order")
    return errors


def validate_result(payload: dict, stage: Stage) -> list[str]:
    schema = _RESULT_SCHEMAS[stage]
    validator = jsonschema.Draft202012Validator(schema)
    errors = [e.message for e in validator.iter_errors(payload)]
    if stage == "analyze" and not errors:
        errors.extend(validate_answer(payload["answer_type"], payload["answer"]))
    return errors
