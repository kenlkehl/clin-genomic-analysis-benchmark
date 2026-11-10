"""JSON-schema contract for question.json (in) and result.json (out).

Agents run only the analyze task, on gold-unambiguous questions. The assess
task is a single prompt with no agent; its reply schema lives in
``assess/schema.py``.
"""

from __future__ import annotations

from typing import Any

import jsonschema

from ..gold_standard.answer_types import validate_answer

CONTRACT_VERSION = "5"


# ---- input (question.json) ----

QUESTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "clin-genomic-analysis-benchmark analyze question",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version", "question_id", "question_text", "cohort",
        "cohort_dir", "data_dictionary_path", "scratch_dir", "instructions",
    ],
    "properties": {
        "contract_version": {"const": CONTRACT_VERSION},
        "question_id": {"type": "string"},
        "question_text": {"type": "string"},
        "cohort": {"type": "string"},
        "cohort_dir": {"type": "string"},
        "data_dictionary_path": {"type": "string"},
        "scratch_dir": {"type": "string"},
        "instructions": {"type": "string"},
        # Optional: budget hint
        "max_runtime_seconds": {"type": "integer", "minimum": 1},
    },
}


# ---- output (result.json) ----

ANALYZE_RESULT_SCHEMA: dict[str, Any] = {
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


def validate_question(payload: dict) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    validator = jsonschema.Draft202012Validator(QUESTION_SCHEMA)
    return [e.message for e in validator.iter_errors(payload)]


def validate_result(payload: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(ANALYZE_RESULT_SCHEMA)
    errors = [e.message for e in validator.iter_errors(payload)]
    if not errors:
        errors.extend(validate_answer(payload["answer_type"], payload["answer"]))
    return errors
