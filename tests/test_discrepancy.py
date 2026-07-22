"""Tests for the per-answer-type discrepancy bands."""

from __future__ import annotations

from clin_genomic_analysis_benchmark.scoring.discrepancy import (
    Band,
    score_categorical,
    score_categorical_distribution,
    score_count,
    score_proportion,
    score_pvalue_bucket,
    score_ratio_log,
)


def test_count_accurate():
    assert score_count({"value": 100}, {"value": 100}).band == Band.ACCURATE
    assert score_count({"value": 102}, {"value": 100}).band == Band.ACCURATE   # 2%
    assert score_count({"value": 95},  {"value": 100}).band == Band.ACCURATE   # 5%


def test_count_minor_major():
    r = score_count({"value": 110}, {"value": 100})    # 10%
    assert r.band == Band.MINOR
    assert r.points == 1
    r = score_count({"value": 130}, {"value": 100})    # 30%
    assert r.band == Band.MAJOR
    assert r.points == 0


def test_proportion_relative():
    assert score_proportion({"value": 0.21}, {"value": 0.20}).band == Band.ACCURATE   # 5%
    assert score_proportion({"value": 0.23}, {"value": 0.20}).band == Band.MINOR      # 15%
    assert score_proportion({"value": 0.40}, {"value": 0.20}).band == Band.MAJOR      # 100%


def test_proportion_absolute_pp():
    r = score_proportion({"value": 0.18}, {"value": 0.20}, mode="absolute_pp",
                         accurate_threshold=0.05, minor_threshold=0.15)
    assert r.band == Band.ACCURATE
    assert r.metric == "absolute_pp"
    r = score_proportion({"value": 0.30}, {"value": 0.20}, mode="absolute_pp",
                         accurate_threshold=0.05, minor_threshold=0.15)
    assert r.band == Band.MINOR


def test_ratio_log_hr():
    # gold HR=2 → log(g)=0.693
    r = score_ratio_log({"value": 2.05}, {"value": 2.0})       # |log(2.05/2)| / log(2) ≈ 3.6%
    assert r.band == Band.ACCURATE
    r = score_ratio_log({"value": 2.5}, {"value": 2.0})        # ~32% on log scale
    assert r.band == Band.MAJOR
    # gold HR ≈ 1: special-case handled (uses log(1+threshold))
    r = score_ratio_log({"value": 1.04}, {"value": 1.0})       # ≤ log(1.05)
    assert r.band == Band.ACCURATE


def test_pvalue_bucket():
    # same bucket
    assert score_pvalue_bucket({"value": 0.0009}, {"value": 0.0001}).band == Band.ACCURATE  # both <0.001
    assert score_pvalue_bucket({"value": 0.30}, {"value": 0.10}).band == Band.ACCURATE       # both ≥0.05
    # off by one
    assert score_pvalue_bucket({"value": 0.04}, {"value": 0.005}).band == Band.MINOR         # <0.05 vs <0.01
    # off by two
    assert score_pvalue_bucket({"value": 0.001 - 1e-9}, {"value": 0.20}).band == Band.MAJOR   # <0.001 vs ≥0.05


def test_categorical_exact_match():
    assert score_categorical({"value": "Female"}, {"value": "female"}).band == Band.ACCURATE
    assert score_categorical({"value": "Male"}, {"value": "Female"}).band == Band.MAJOR


def test_categorical_distribution_all_accurate():
    gold = {"proportions_by_category": {"A": 0.40, "B": 0.35, "C": 0.25}}
    agent = {"proportions_by_category": {"A": 0.41, "B": 0.34, "C": 0.25}}  # max ~3% rel
    r = score_categorical_distribution(agent, gold)
    assert r.band == Band.ACCURATE


def test_categorical_distribution_worst_drags_band():
    # Big categories nearly exact; small "Other" off by 30% relative
    gold = {"proportions_by_category": {"A": 0.45, "B": 0.45, "Other": 0.10}}
    agent = {"proportions_by_category": {"A": 0.45, "B": 0.42, "Other": 0.13}}  # Other 30% rel
    r = score_categorical_distribution(agent, gold)
    assert r.band == Band.MAJOR
    assert "Other" in r.explanation


def test_categorical_distribution_minor_band():
    gold = {"proportions_by_category": {"A": 0.50, "B": 0.50}}
    agent = {"proportions_by_category": {"A": 0.55, "B": 0.45}}  # 10% rel on A, 10% on B
    r = score_categorical_distribution(agent, gold)
    assert r.band == Band.MINOR


def test_categorical_distribution_key_mismatch_major():
    gold = {"proportions_by_category": {"A": 0.5, "B": 0.5}}
    agent = {"proportions_by_category": {"A": 0.5, "Unknown": 0.5}}
    r = score_categorical_distribution(agent, gold)
    assert r.band == Band.MAJOR
    assert r.metric == "category_key_mismatch"


def test_categorical_distribution_absolute_pp_mode():
    gold = {"proportions_by_category": {"A": 0.40, "B": 0.60}}
    agent = {"proportions_by_category": {"A": 0.43, "B": 0.57}}  # 3pp off
    r = score_categorical_distribution(agent, gold, mode="absolute_pp",
                                       accurate_threshold=0.05, minor_threshold=0.15)
    assert r.band == Band.ACCURATE
    assert r.metric == "max_per_category_absolute_pp"
