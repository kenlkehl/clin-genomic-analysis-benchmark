"""Tests for the component registry and the gold component schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clin_genomic_analysis_benchmark.components import (
    COMPONENT_IDS,
    classification_from_flags,
    components_payload,
    flags_from_assess_result,
)
from clin_genomic_analysis_benchmark.questions.schema import ComponentAssessment, Question


def test_component_ids_are_fixed_and_ordered() -> None:
    assert COMPONENT_IDS == ("denominator", "outcome", "covariates", "analysis_plan")
    assert [item["id"] for item in components_payload()] == list(COMPONENT_IDS)


def test_any_flag_makes_the_question_ambiguous() -> None:
    clear = {cid: False for cid in COMPONENT_IDS}
    assert classification_from_flags(clear) == "unambiguous"
    assert classification_from_flags({**clear, "covariates": True}) == "ambiguous"
    with pytest.raises(ValueError, match="missing component verdicts"):
        classification_from_flags({"denominator": False})


def test_flags_require_booleans() -> None:
    result = {cid: {"needs_clarification": False} for cid in COMPONENT_IDS}
    assert flags_from_assess_result(result) == {cid: False for cid in COMPONENT_IDS}
    with pytest.raises(ValueError, match="outcome"):
        flags_from_assess_result({**result, "outcome": {"needs_clarification": "yes"}})


def _assessment(**flags: bool) -> ComponentAssessment:
    return ComponentAssessment(**{
        cid: {"needs_clarification": flags.get(cid, False)} for cid in COMPONENT_IDS})


def test_gold_classification_must_match_component_verdicts() -> None:
    q = Question(id="c-Q1", category=1, text="t", classification="ambiguous",
                 component_assessment=_assessment(outcome=True))
    assert q.component_assessment.flags()["outcome"] is True
    with pytest.raises(ValidationError, match="disagrees with the component verdicts"):
        Question(id="c-Q2", category=1, text="t", classification="unambiguous",
                 component_assessment=_assessment(outcome=True))


def test_unreviewed_legacy_gold_still_loads() -> None:
    q = Question(id="c-Q3", category=1, text="t", classification="ambiguous",
                 disambiguation_concept_ids=["TREATMENT_SETTING"],
                 disambiguation_concepts=["legacy prose"])
    assert q.component_assessment is None
