"""Tests for score-run driver edge cases."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from clin_genomic_analysis_benchmark.questions.schema import CohortQuestionFile, Question
from clin_genomic_analysis_benchmark.scoring import driver


def test_ambiguous_gold_unambiguous_agent_does_not_claim_missing_gold(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "agent_name": "agent",
        "run_id": "run-1",
        "cohorts": ["cohort_1"],
    }))
    (run_dir / "runs.json").write_text(json.dumps([{
        "cohort": "cohort_1",
        "question_id": "cohort_1-Q1",
        "category": 1,
        "classify": {
            "success": True,
            "result": {"classification": "unambiguous"},
        },
        "analyze": {
            "success": True,
            "result": {
                "answer_type": "count",
                "answer": {"value": 10},
            },
        },
    }]))

    gold = CohortQuestionFile(
        cohort="cohort_1",
        generated_at=datetime.now(timezone.utc),
        model="test",
        questions=[
            Question(
                id="cohort_1-Q1",
                category=1,
                text="Which treatment is best?",
                classification="ambiguous",
                disambiguation_concepts=["outcome definition"],
            )
        ],
    )
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: gold)

    driver.score_run(run_path=str(run_dir))

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    reason = scorecard["questions"][0]["failure_reason"]
    assert reason == (
        "gold question is ambiguous; agent classified it unambiguous, "
        "so analysis was not scored"
    )
    assert scorecard["questions"][0]["gold_disambiguation_n"] == 1
    assert scorecard["overall"]["points"] == 0
    # 1 pt classify + 1 gold concept x 4 (two judges x 2 pts).
    assert scorecard["overall"]["points_possible"] == 5
    assert scorecard["overall"]["mean_concept_recall"] == 0
    assert "compute-gold" not in (run_dir / "scorecard.md").read_text()

def test_subtask_weighting_is_equal_thirds_by_default():
    """Headline is the mean of three self-normalised subtask scores, not a point sum."""
    from clin_genomic_analysis_benchmark.scoring.aggregator import QuestionScore, aggregate
    from clin_genomic_analysis_benchmark.scoring.classification import ClassificationResult
    from clin_genomic_analysis_benchmark.scoring.discrepancy import Band, DiscrepancyResult
    from clin_genomic_analysis_benchmark.scoring.types import DisambiguationScoreResult

    # One ambiguous question: classify right, half the disambiguation points.
    amb = QuestionScore(
        question_id="a", cohort="c", category=1, gold_classification="ambiguous",
        gold_disambiguation_n=2, disambig_points_per_concept=4.0,
        classification=ClassificationResult(True, 1, "ambiguous", "ambiguous"),
        disambiguation=DisambiguationScoreResult(
            question_id="a", cohort="c", n_gold=2, points=4.0, decisions=[],
            points_per_concept=4.0),
    )
    # One unambiguous question: classify wrong, no analysis attempted.
    unamb = QuestionScore(
        question_id="b", cohort="c", category=1, gold_classification="unambiguous",
        classification=ClassificationResult(False, 0, "ambiguous", "unambiguous"),
    )
    overall, _ = aggregate([amb, unamb])

    assert overall.subtask_scores["classify"] == 0.5        # 1 of 2
    assert overall.subtask_scores["disambiguate"] == 0.5    # 4 of 8
    assert overall.subtask_scores["analyze"] == 0.0         # 0 of 2, never attempted
    assert abs(overall.overall_score - (0.5 + 0.5 + 0.0) / 3) < 1e-9


def test_subtask_weights_are_configurable_and_renormalised():
    from clin_genomic_analysis_benchmark.scoring.aggregator import QuestionScore, aggregate
    from clin_genomic_analysis_benchmark.scoring.classification import ClassificationResult

    q = QuestionScore(
        question_id="a", cohort="c", category=1, gold_classification="unambiguous",
        classification=ClassificationResult(True, 1, "unambiguous", "unambiguous"),
    )
    # No ambiguous questions at all: disambiguate drops out and the remaining
    # weights renormalise rather than scoring it as zero.
    overall, _ = aggregate([q])
    assert overall.subtask_scores["disambiguate"] is None
    assert abs(overall.overall_score - (1.0 + 0.0) / 2) < 1e-9

    overall, _ = aggregate([q], subtask_weights={"classify": 0.8, "analyze": 0.2})
    assert abs(overall.overall_score - (0.8 * 1.0 + 0.2 * 0.0)) < 1e-9
