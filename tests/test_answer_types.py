"""Tests for the gold-standard answer-type validator."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.gold_standard.answer_types import validate_answer


def test_proportion_valid():
    assert validate_answer("proportion", {
        "value": 0.5, "numerator": 1, "denominator": 2,
        "ci_low": 0.2, "ci_high": 0.8,
    }) == []


def test_proportion_out_of_range():
    errs = validate_answer("proportion", {
        "value": 1.5, "numerator": 3, "denominator": 2,
    })
    assert errs, "value > 1 must error"


def test_hr_required_fields():
    errs = validate_answer("hazard_ratio_with_ci", {"value": 1.5})
    assert any("missing" in e for e in errs)


def test_pvalue_requires_test_name():
    errs = validate_answer("pvalue", {"value": 0.05})
    assert any("test_name" in e for e in errs)


def test_categorical_requires_string():
    errs = validate_answer("categorical", {"value": ""})
    assert errs
    assert validate_answer("categorical", {"value": "Female"}) == []


def test_unknown_answer_type():
    errs = validate_answer("frobnicate", {"value": 1})
    assert any("unknown answer_type" in e for e in errs)


def test_categorical_distribution_valid():
    assert validate_answer("categorical_distribution", {
        "proportions_by_category": {"A": 0.4, "B": 0.35, "C": 0.25},
        "counts_by_category": {"A": 40, "B": 35, "C": 25},
        "denominator": 100,
    }) == []


def test_categorical_distribution_must_sum_to_one():
    errs = validate_answer("categorical_distribution", {
        "proportions_by_category": {"A": 0.4, "B": 0.4},
    })
    assert any("sum to 1.0" in e for e in errs)


def test_categorical_distribution_counts_match_denominator():
    errs = validate_answer("categorical_distribution", {
        "proportions_by_category": {"A": 0.5, "B": 0.5},
        "counts_by_category": {"A": 5, "B": 5},
        "denominator": 11,
    })
    assert any("denominator" in e for e in errs)


def test_categorical_distribution_counts_keys_match_proportions():
    errs = validate_answer("categorical_distribution", {
        "proportions_by_category": {"A": 0.5, "B": 0.5},
        "counts_by_category": {"A": 5, "X": 5},
    })
    assert any("keys must match" in e for e in errs)


def test_categorical_distribution_requires_proportions():
    errs = validate_answer("categorical_distribution", {"counts_by_category": {"A": 1}})
    assert any("proportions_by_category" in e for e in errs)
