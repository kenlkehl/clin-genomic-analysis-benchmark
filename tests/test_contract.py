"""Tests for the analyze agent contract and the assess reply schema."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.agent.contract import (
    CONTRACT_VERSION,
    validate_question,
    validate_result,
)
from clin_genomic_analysis_benchmark.assess.schema import validate_assess_result
from clin_genomic_analysis_benchmark.components import COMPONENT_IDS


def _q() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "question_id": "bladder_1.2-Qabc12345",
        "question_text": "What is the most common race?",
        "cohort": "bladder_1.2",
        "cohort_dir": "/abs/path",
        "data_dictionary_path": "/abs/dict",
        "scratch_dir": "/abs/scratch",
        "instructions": "do the thing",
    }


def _assess(**flags: bool) -> dict:
    return {
        cid: {"needs_clarification": flags.get(cid, False), "rationale": "..."}
        for cid in COMPONENT_IDS
    }


def test_question_schema_valid():
    assert validate_question(_q()) == []
    budget = _q()
    budget["max_runtime_seconds"] = 60
    assert validate_question(budget) == []


def test_question_schema_rejects_retired_fields():
    """The agent contract is analyze-only: no stage, components, or route."""
    for key, value in (
        ("stage", "analyze"),
        ("assessment_components", []),
        ("prior_classification", "unambiguous"),
        ("category", 1),
        ("disambiguation_concept_menu", []),
    ):
        bad = _q()
        bad[key] = value
        assert validate_question(bad), f"should reject {key!r}"


def test_question_schema_rejects_old_contract():
    for version in ("3", "4"):
        old = _q()
        old["contract_version"] = version
        assert validate_question(old)


def test_analyze_result_valid():
    r = {
        "answer_type": "proportion",
        "answer": {
            "value": 0.5, "numerator": 1, "denominator": 2,
            "ci_low": 0.1, "ci_high": 0.9,
        },
        "methods": "x",
    }
    assert validate_result(r) == []
    assert validate_result({"answer_type": "weird", "answer": {}})


def test_analyze_result_rejects_an_assess_reply():
    assert validate_result(_assess())


def test_assess_result_valid():
    assert validate_assess_result(_assess()) == []
    assert validate_assess_result(_assess(outcome=True, analysis_plan=True)) == []
    no_rationale = {cid: {"needs_clarification": False} for cid in COMPONENT_IDS}
    assert validate_assess_result(no_rationale) == [], "rationale is optional"


def test_assess_result_rejects_missing_component():
    for cid in COMPONENT_IDS:
        bad = _assess()
        del bad[cid]
        assert validate_assess_result(bad), f"should reject missing {cid}"


def test_assess_result_rejects_non_boolean_verdict():
    for value in ("yes", 1, None):
        bad = _assess()
        bad["denominator"]["needs_clarification"] = value
        assert validate_assess_result(bad), f"should reject {value!r}"
    missing = _assess()
    del missing["covariates"]["needs_clarification"]
    assert validate_assess_result(missing)


def test_assess_result_rejects_extra_properties():
    top = _assess()
    top["classification"] = "ambiguous"
    assert validate_assess_result(top)
    nested = _assess()
    nested["outcome"]["concept_ids"] = ["TIME_ORIGIN"]
    assert validate_assess_result(nested)
