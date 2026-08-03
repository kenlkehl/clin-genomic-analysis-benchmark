"""Tests for the agent-harness contract JSON schemas."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.agent.contract import (
    validate_question,
    validate_result,
)
from clin_genomic_analysis_benchmark.concepts import concept_menu_payload


def _q(stage: str = "classify") -> dict:
    return {
        "contract_version": "2",
        "question_id": "bladder_1.2-Qabc12345",
        "question_text": "What is the most common race?",
        "cohort": "bladder_1.2",
        "category": 1,
        "stage": stage,
        "cohort_dir": "/abs/path",
        "data_dictionary_path": "/abs/dict",
        "scratch_dir": "/abs/scratch",
        "instructions": "do the thing",
        "disambiguation_concept_menu": concept_menu_payload(),
    }


def test_question_schema_valid():
    assert validate_question(_q("classify")) == []
    assert validate_question(_q("disambiguate")) == []
    assert validate_question(_q("analyze")) == []


def test_question_schema_rejects_bad_stage():
    bad = _q("classify")
    bad["stage"] = "frobnicate"
    errs = validate_question(bad)
    assert errs, "should reject unknown stage"


def test_classify_result_valid():
    r = {"classification": "ambiguous", "rationale": "..."}
    assert validate_result(r, "classify") == []
    bad = {"classification": "maybe"}
    assert validate_result(bad, "classify"), "should reject bad enum"


def test_disambiguate_result_valid():
    valid = {"concept_ids": ["OUTCOME_METRIC", "TIME_ORIGIN"]}
    assert validate_result(valid, "disambiguate") == []
    assert validate_result({"concept_ids": []}, "disambiguate"), "empty list rejected"
    assert validate_result({"concept_ids": ["FREE TEXT"]}, "disambiguate")
    assert validate_result({"concept_ids": ["TIME_ORIGIN", "TIME_ORIGIN"]}, "disambiguate")


def test_analyze_result_valid():
    r = {
        "answer_type": "proportion",
        "answer": {
            "value": 0.5, "numerator": 1, "denominator": 2,
            "ci_low": 0.1, "ci_high": 0.9,
        },
        "methods": "x",
    }
    assert validate_result(r, "analyze") == []
    bad = {"answer_type": "weird", "answer": {}}
    assert validate_result(bad, "analyze")


def test_stage_results_reject_extra_properties():
    assert validate_result({"classification": "ambiguous", "extra": 1}, "classify")
    assert validate_result({"concept_ids": ["TIME_ORIGIN"], "rationale": "x"},
                           "disambiguate")
