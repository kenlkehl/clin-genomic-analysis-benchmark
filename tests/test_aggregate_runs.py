from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from aggregate_runs import CSV_FIELDS, aggregate_runs
from clin_genomic_analysis_benchmark.components import COMPONENT_IDS


def _components(balanced: float | None) -> dict[str, dict]:
    return {
        cid: {
            "balanced_accuracy": balanced,
            "sensitivity": None if balanced is None else min(1.0, balanced + 0.1),
            "specificity": None if balanced is None else max(0.0, balanced - 0.1),
        }
        for cid in COMPONENT_IDS
    }


def _write_manifest(path: Path, failed_after_retries: int, history: str) -> None:
    targets = [
        {"cohort": "test-cohort", "question_id": f"question-{index}", "merged": False}
        for index in range(failed_after_retries)
    ]
    path.with_name("manifest.json").write_text(
        json.dumps({history: [{"targets": targets}] if failed_after_retries else []}),
        encoding="utf-8",
    )


def _assess_overall(score: float | None, balanced: float | None = 0.7,
                    question_level: float | None = 0.8) -> dict:
    return {"n": 205, "score": score, "classify_accuracy": question_level,
            "components": _components(balanced)}


def _write_assess(path: Path, *, run_name: str, run_id: str, score: float | None,
                  balanced: float | None = 0.7, failed_after_retries: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scorer": "deterministic-rules-v4",
        "task": "assess",
        "run_name": run_name,
        "run_id": run_id,
        "provenance": {"model": "assess-model", "provider": "vertex-anthropic"},
        "integrity": {"status": "valid", "boundary": "prompt_only"},
        "overall": _assess_overall(score, balanced),
    }), encoding="utf-8")
    _write_manifest(path, failed_after_retries, "retry_history")


def _write_analyze(path: Path, *, run_name: str, run_id: str, score: float | None,
                   failed_after_retries: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scorer": "deterministic-rules-v4",
        "task": "analyze",
        "run_name": run_name,
        "run_id": run_id,
        "provenance": {"model": "agent-model", "provider": "google_vertex_ai",
                       "effort_level": "high"},
        "integrity": {"status": "valid"},
        "overall": {"n": 106, "score": score, "points": 0, "possible": 212},
    }), encoding="utf-8")
    _write_manifest(path, failed_after_retries, "repair_history")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_aggregate_runs_reads_each_task_from_active_two_level_scorecards(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _write_assess(runs_dir / "assess_a" / "run-1" / "scorecard.json",
                  run_name="assess_a", run_id="run-1", score=0.6254, balanced=None)
    _write_assess(runs_dir / "assess_b" / "run-2" / "scorecard.json",
                  run_name="assess_b", run_id="run-2", score=0.8, failed_after_retries=2)
    _write_analyze(runs_dir / "agent-c" / "run-3" / "scorecard.json",
                   run_name="agent-c", run_id="run-3", score=0.5, failed_after_retries=1)
    _write_assess(runs_dir / "failed_runs" / "assess_x" / "run" / "scorecard.json",
                  run_name="assess_x", run_id="run", score=1.0)
    _write_assess(runs_dir / "assess_a" / "pre_repair" / "raw" / "scorecard.json",
                  run_name="assess_a", run_id="raw", score=1.0)

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 3

    rows = _rows(output_path)
    assert tuple(rows[0]) == CSV_FIELDS
    assert [(row["task"], row["run_name"]) for row in rows] == [
        ("analyze", "agent-c"), ("assess", "assess_b"), ("assess", "assess_a")]
    expected = {
        "task": "assess",
        "run_name": "assess_b",
        "run_id": "run-2",
        "model": "assess-model",
        "provider": "vertex-anthropic",
        "effort": "",
        "integrity_status": "valid",
        "questions_scored": "205",
        "questions_failed_after_retries": "2",
        "questions_completed_successfully": "203",
        "score_pct": "80.0",
        "assess_score_pct": "80.0",
        "analyze_score_pct": "",
        "question_level_accuracy_pct": "80.0",
        "scorecard": "assess_b/run-2/scorecard.json",
    }
    for cid in COMPONENT_IDS:
        expected[f"{cid}_balanced_accuracy_pct"] = "70.0"
        expected[f"{cid}_sensitivity_pct"] = "80.0"
        expected[f"{cid}_specificity_pct"] = "60.0"
    assert rows[1] == expected
    assert rows[2]["score_pct"] == "62.5"
    assert rows[2]["denominator_balanced_accuracy_pct"] == ""
    analyze = rows[0]
    assert analyze["score_pct"] == analyze["analyze_score_pct"] == "50.0"
    assert analyze["assess_score_pct"] == ""
    assert analyze["denominator_balanced_accuracy_pct"] == ""
    assert analyze["effort"] == "high"
    assert analyze["questions_failed_after_retries"] == "1"
    assert analyze["questions_completed_successfully"] == "105"
    assert b"\r\n" not in output_path.read_bytes()


def test_aggregate_runs_reads_combined_scorecards(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    path = runs_dir / "combined" / "c1" / "scorecard.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "scorer": "deterministic-rules-v4",
        "task": "combined",
        "run_name": "combined",
        "run_id": "c1",
        "sources": {
            "assess": {"provenance": {"model": "assess-model", "provider": "p"},
                       "integrity_status": "valid"},
            "analyze": {"provenance": {"model": "agent-model", "provider": "p",
                                       "effort_level": "xhigh"},
                        "integrity_status": "valid"},
        },
        "overall": {"score": 0.65, "subtask_scores": {"assess": 0.8, "analyze": 0.5},
                    "assess": _assess_overall(0.8), "analyze": {"n": 106, "score": 0.5}},
    }), encoding="utf-8")

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 1

    row = _rows(output_path)[0]
    assert row["task"] == "combined"
    assert row["model"] == "assess-model | agent-model"
    assert row["provider"] == "p"
    assert row["effort"] == "xhigh"
    assert (row["score_pct"], row["assess_score_pct"], row["analyze_score_pct"]) == (
        "65.0", "80.0", "50.0")
    assert row["question_level_accuracy_pct"] == "80.0"
    assert row["denominator_balanced_accuracy_pct"] == "70.0"
    assert row["questions_scored"] == "205"


def test_aggregate_runs_skips_scorecards_from_before_the_split(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    _write_assess(runs_dir / "assess_a" / "run-1" / "scorecard.json",
                  run_name="assess_a", run_id="run-1", score=0.5)
    for scorer in ("deterministic-rules-v2", "deterministic-rules-v3"):
        old = runs_dir / f"agent-{scorer}" / "run-old" / "scorecard.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({"scorer": scorer, "agent_name": "a", "run_id": "r",
                                   "overall": {"n": 205}}), encoding="utf-8")

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 1

    assert [row["run_name"] for row in _rows(output_path)] == ["assess_a"]
    err = capsys.readouterr().err
    assert "Skipped 2 scorecard(s)" in err
    assert "deterministic-rules-v3" in err


def test_aggregate_runs_sorts_unscored_runs_last_within_a_task(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_assess(runs_dir / "assess_a" / "run-1" / "scorecard.json",
                  run_name="assess_a", run_id="run-1", score=None)
    _write_assess(runs_dir / "assess_b" / "run-2" / "scorecard.json",
                  run_name="assess_b", run_id="run-2", score=0.1)

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 2
    assert [row["run_name"] for row in _rows(output_path)] == ["assess_b", "assess_a"]


def test_aggregate_runs_rejects_malformed_scorecard(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    scorecard = runs_dir / "agent-a" / "run-1" / "scorecard.json"
    scorecard.parent.mkdir(parents=True)
    scorecard.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="could not read scorecard"):
        aggregate_runs(runs_dir, tmp_path / "scores.csv")


def test_aggregate_runs_rejects_assess_scorecard_missing_components(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    scorecard = runs_dir / "assess_a" / "run-1" / "scorecard.json"
    _write_assess(scorecard, run_name="assess_a", run_id="run-1", score=0.5)
    payload = json.loads(scorecard.read_text(encoding="utf-8"))
    del payload["overall"]["components"]["covariates"]
    scorecard.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="overall.components.covariates"):
        aggregate_runs(runs_dir, tmp_path / "scores.csv")


def test_aggregate_runs_reads_scorecards_the_scorer_writes(tmp_path: Path, monkeypatch) -> None:
    """The CSV reader and the real scorer agree on the scorecard shape."""
    from datetime import datetime, timezone

    from clin_genomic_analysis_benchmark.questions.schema import (
        CohortQuestionFile,
        ComponentAssessment,
        Question,
    )
    from clin_genomic_analysis_benchmark.scoring import combine, driver

    gold = CohortQuestionFile(
        cohort="c", generated_at=datetime.now(timezone.utc), model="m",
        questions=[Question(
            id="c-Q1", category=1, text="How many?", classification="unambiguous",
            component_assessment=ComponentAssessment(**{
                cid: {"needs_clarification": False} for cid in COMPONENT_IDS}),
            analysis_spec={"population_unit": "patient", "tables": ["t"],
                           "statistic": "count", "expected_answer_type": "count"},
            gold_answer={"value": 10},
        )])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: gold)
    monkeypatch.setattr(combine, "RUNS_DIR", tmp_path / "runs")
    runs_dir = tmp_path / "runs"
    verdicts = {cid: {"needs_clarification": False} for cid in COMPONENT_IDS}
    for task, name, record in (
        ("assess", "assess_m", {"assess": {"success": True, "result": verdicts}}),
        ("analyze", "agent", {"analyze": {"success": True, "result": {
            "answer_type": "count", "answer": {"value": 10}}}}),
    ):
        run_dir = runs_dir / name / "r"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "task": task, "run_id": "r", "cohorts": ["c"], "run_name": name,
            "agent_name": name, "integrity": {"status": "valid"}, "settings": {}}))
        (run_dir / "runs.json").write_text(json.dumps([
            {"cohort": "c", "question_id": "c-Q1", "category": 1, **record}]))
        driver.score_run(run_path=str(run_dir))
    combine.combine_runs(assess_run=runs_dir / "assess_m" / "r",
                         analyze_run=runs_dir / "agent" / "r", run_id="c1")

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 3
    by_task = {row["task"]: row for row in _rows(output_path)}
    assert by_task["analyze"]["score_pct"] == "100.0"
    assert by_task["assess"]["question_level_accuracy_pct"] == "100.0"
    assert by_task["combined"]["score_pct"] != ""
