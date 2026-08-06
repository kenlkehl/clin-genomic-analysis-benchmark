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
