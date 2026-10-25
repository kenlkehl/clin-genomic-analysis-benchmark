"""Tests for the canonical disambiguation concept menu and legacy mapping."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.concepts import (
    CONCEPT_IDS,
    infer_legacy_concept_ids,
)


def test_subgroup_and_category_definitions_share_one_concept() -> None:
    assert "CLINICAL_SUBGROUP_DEFINITION" in CONCEPT_IDS
    assert "VARIABLE_OR_CATEGORY_DEFINITION" not in CONCEPT_IDS
    assert infer_legacy_concept_ids("subtype restriction (TNBC vs HR+)") == [
        "CLINICAL_SUBGROUP_DEFINITION"
    ]
    assert infer_legacy_concept_ids(
        "race vs ethnicity field choice (NAACCR vs derived)"
    ) == ["CLINICAL_SUBGROUP_DEFINITION"]


def test_treatment_setting_folds_into_line_of_therapy() -> None:
    assert "LINE_OF_THERAPY" in CONCEPT_IDS
    assert "TREATMENT_SETTING" not in CONCEPT_IDS
    assert infer_legacy_concept_ids(
        "treatment setting (adjuvant vs. neoadjuvant vs. metastatic vs. any setting)"
    ) == ["LINE_OF_THERAPY"]
    assert infer_legacy_concept_ids(
        "line of therapy / treatment setting (first-line vs any line)"
    ) == ["LINE_OF_THERAPY"]
