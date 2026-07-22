"""Tests for the agent-harness contract JSON schemas."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.agent.contract import (
    validate_question,
    validate_result,
)


def _q(stage: str = "classify") -> dict:
    return {
        "question_id": "bladder_1.2-Qabc12345",
        "question_text": "What is the most common race?",
        "cohort": "bladder_1.2",
        "category": 1,
        "stage": stage,
        "cohort_dir": "/abs/path",
        "data_dictionary_path": "/abs/dict",
        "scratch_dir": "/abs/scratch",
        "instructions": "do the thing",
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
    assert validate_result({"concepts": ["a", "b"]}, "disambiguate") == []
    assert validate_result({"concepts": []}, "disambiguate"), "empty list rejected"


def test_analyze_result_valid():
    r = {
        "answer_type": "proportion",
        "answer": {"value": 0.5, "numerator": 1, "denominator": 2},
        "methods": "x",
    }
    assert validate_result(r, "analyze") == []
    bad = {"answer_type": "weird", "answer": {}}
    assert validate_result(bad, "analyze")
