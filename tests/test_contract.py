"""Tests for the agent-harness contract JSON schemas."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.agent.contract import (
    validate_question,
    validate_result,
)
from clin_genomic_analysis_benchmark.components import COMPONENT_IDS, components_payload


def _q(stage: str = "assess") -> dict:
    return {
        "contract_version": "3",
        "question_id": "bladder_1.2-Qabc12345",
        "question_text": "What is the most common race?",
        "cohort": "bladder_1.2",
        "category": 1,
        "stage": stage,
        "cohort_dir": "/abs/path",
        "data_dictionary_path": "/abs/dict",
        "scratch_dir": "/abs/scratch",
        "instructions": "do the thing",
        "assessment_components": components_payload(),
    }


def _assess(**flags: bool) -> dict:
    return {
        cid: {"needs_clarification": flags.get(cid, False), "rationale": "..."}
        for cid in COMPONENT_IDS
    }


def test_question_schema_valid():
    assert validate_question(_q("assess")) == []
    assert validate_question(_q("analyze")) == []


def test_question_schema_rejects_bad_stage():
    for stage in ("frobnicate", "classify", "disambiguate"):
        bad = _q("assess")
        bad["stage"] = stage
        assert validate_question(bad), f"should reject stage {stage!r}"


def test_question_schema_rejects_old_contract_and_menu():
    old = _q("assess")
    old["contract_version"] = "2"
    assert validate_question(old)
    menu = _q("assess")
    menu["disambiguation_concept_menu"] = []
    assert validate_question(menu), "concept menu is no longer part of the contract"


def test_question_schema_requires_canonical_components_in_order():
    missing = _q("assess")
    del missing["assessment_components"]
    assert validate_question(missing)
    reordered = _q("assess")
    reordered["assessment_components"] = list(reversed(components_payload()))
    assert any("canonical components" in e for e in validate_question(reordered))


def test_assess_result_valid():
    assert validate_result(_assess(), "assess") == []
    assert validate_result(_assess(outcome=True, analysis_plan=True), "assess") == []
    no_rationale = {cid: {"needs_clarification": False} for cid in COMPONENT_IDS}
    assert validate_result(no_rationale, "assess") == [], "rationale is optional"


def test_assess_result_rejects_missing_component():
    for cid in COMPONENT_IDS:
        bad = _assess()
        del bad[cid]
        assert validate_result(bad, "assess"), f"should reject missing {cid}"


def test_assess_result_rejects_non_boolean_verdict():
    for value in ("yes", 1, None):
        bad = _assess()
        bad["denominator"]["needs_clarification"] = value
        assert validate_result(bad, "assess"), f"should reject {value!r}"
    missing = _assess()
    del missing["covariates"]["needs_clarification"]
    assert validate_result(missing, "assess")


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
    top = _assess()
    top["classification"] = "ambiguous"
    assert validate_result(top, "assess")
    nested = _assess()
    nested["outcome"]["concept_ids"] = ["TIME_ORIGIN"]
    assert validate_result(nested, "assess")
