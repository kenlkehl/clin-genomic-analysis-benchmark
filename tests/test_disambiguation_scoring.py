"""Tests for exact concept-ID scoring and false-positive penalties."""

from __future__ import annotations

import pytest

from clin_genomic_analysis_benchmark.scoring.disambiguation import score_disambiguation


GOLD = ["OUTCOME_METRIC", "TIME_ORIGIN", "DELAYED_ENTRY"]


def _score(agent, **kwargs):
    return score_disambiguation(
        question_id="q",
        cohort="cohort",
        gold_concept_ids=GOLD,
        agent_concept_ids=agent,
        **kwargs,
    )


def test_exact_set_earns_full_credit():
    result = _score(GOLD)
    assert result.points == result.points_possible == 3.0
    assert result.correct_concept_ids == GOLD
    assert result.incorrect_concept_ids == []
    assert result.missed_concept_ids == []


def test_missing_concept_loses_available_point():
    result = _score(["OUTCOME_METRIC", "TIME_ORIGIN"])
    assert result.points == 2.0
    assert result.missed_concept_ids == ["DELAYED_ENTRY"]
    assert result.recall == pytest.approx(2 / 3)


def test_incorrect_concept_subtracts_configured_penalty():
    result = _score(["OUTCOME_METRIC", "TIME_ORIGIN", "MODEL_SPECIFICATION"])
    assert result.raw_points_before_floor == 1.75
    assert result.points == 1.75
    assert result.incorrect_concept_ids == ["MODEL_SPECIFICATION"]
    assert result.precision == pytest.approx(2 / 3)


def test_points_floor_at_zero():
    result = _score([
        "MODEL_SPECIFICATION",
        "CENSORING_RULE",
        "PANEL_COVERAGE",
        "ALTERATION_TYPE",
        "CNA_THRESHOLD",
    ], incorrect_concept_penalty=1.0)
    assert result.raw_points_before_floor == -5.0
    assert result.points == 0.0


def test_penalty_is_configurable():
    result = _score(["OUTCOME_METRIC", "MODEL_SPECIFICATION"],
                    incorrect_concept_penalty=0.5)
    assert result.points == 0.5


def test_empty_selection_scores_zero_and_records_all_missed():
    result = _score([])
    assert result.points == 0.0
    assert result.missed_concept_ids == GOLD


def test_unknown_or_duplicate_ids_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        _score(["NOT_A_MENU_ID"])
    with pytest.raises(ValueError, match="unique"):
        _score(["OUTCOME_METRIC", "OUTCOME_METRIC"])
