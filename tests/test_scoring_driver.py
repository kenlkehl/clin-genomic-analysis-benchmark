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
    assert scorecard["overall"]["points_possible"] == 2
    assert scorecard["overall"]["mean_concept_recall"] == 0
    assert "compute-gold" not in (run_dir / "scorecard.md").read_text()
