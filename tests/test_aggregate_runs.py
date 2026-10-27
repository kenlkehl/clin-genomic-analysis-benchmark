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


def _write_manifest(path: Path, failed_after_retries: int) -> None:
    path.with_name("manifest.json").write_text(
        json.dumps(
            {
                "repair_history": [
                    {
                        "targets": [
                            {
                                "cohort": "test-cohort",
                                "question_id": f"question-{index}",
                                "merged": False,
                            }
                            for index in range(failed_after_retries)
                        ]
                    }
                ]
                if failed_after_retries
                else []
            }
        ),
        encoding="utf-8",
    )


def _write_scorecard(
    path: Path,
    *,
    agent: str,
    run_id: str,
    overall: float | None,
    assess: float | None,
    analyze: float | None,
    balanced: float | None = 0.7,
    question_level: float | None = 0.8,
    failed_after_retries: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_name": agent,
                "run_id": run_id,
                "scorer": "deterministic-rules-v3",
                "agent_provenance": {
                    "model": "test-model",
                    "provider": "test-provider",
                    "effort_level": "high",
                },
                "integrity": {"status": "valid"},
                "overall": {
                    "n": 205,
                    "overall_score": overall,
                    "classify_accuracy": question_level,
                    "subtask_scores": {"assess": assess, "analyze": analyze},
                    "components": _components(balanced),
                },
            }
        ),
        encoding="utf-8",
    )
    _write_manifest(path, failed_after_retries)


def _write_v2_scorecard(path: Path, *, agent: str, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_name": agent,
                "run_id": run_id,
                "scorer": "deterministic-rules-v2",
                "integrity": {"status": "valid"},
                "overall": {
                    "n": 211,
                    "overall_score": 0.99,
                    "subtask_scores": {
                        "classify": 0.99,
                        "disambiguate": 0.99,
                        "analyze": 0.99,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _write_manifest(path, 0)


def test_aggregate_runs_includes_only_active_two_level_scorecards(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _write_scorecard(
        runs_dir / "agent-a" / "run-1" / "scorecard.json",
        agent="agent-a",
        run_id="run-1",
        overall=0.6254,
        assess=0.75,
        analyze=0.5,
        balanced=None,
    )
    _write_scorecard(
        runs_dir / "agent-b" / "run-2" / "scorecard.json",
        agent="agent-b",
        run_id="run-2",
        overall=0.8,
        assess=0.9,
        analyze=0.7,
        failed_after_retries=2,
    )
    _write_scorecard(
        runs_dir / "failed_runs" / "agent-c" / "run-3" / "scorecard.json",
        agent="agent-c",
        run_id="run-3",
        overall=1.0,
        assess=1.0,
        analyze=1.0,
    )
    _write_scorecard(
        runs_dir / "agent-a" / "pre_repair" / "raw-run" / "scorecard.json",
        agent="agent-a",
        run_id="raw-run",
        overall=1.0,
        assess=1.0,
        analyze=1.0,
    )

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 2

    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["agent"] for row in rows] == ["agent-b", "agent-a"]
    assert tuple(rows[0]) == CSV_FIELDS
    expected = {
        "agent": "agent-b",
        "run_id": "run-2",
        "model": "test-model",
        "provider": "test-provider",
        "effort": "high",
        "integrity_status": "valid",
        "questions_scored": "205",
        "questions_failed_after_retries": "2",
        "questions_completed_successfully": "203",
        "overall_score_pct": "80.0",
        "assess_score_pct": "90.0",
        "analyze_score_pct": "70.0",
        "question_level_accuracy_pct": "80.0",
        "scorecard": "agent-b/run-2/scorecard.json",
    }
    for cid in COMPONENT_IDS:
        expected[f"{cid}_balanced_accuracy_pct"] = "70.0"
        expected[f"{cid}_sensitivity_pct"] = "80.0"
        expected[f"{cid}_specificity_pct"] = "60.0"
    assert rows[0] == expected
    assert rows[1]["overall_score_pct"] == "62.5"
    assert rows[1]["denominator_balanced_accuracy_pct"] == ""
    assert rows[1]["questions_failed_after_retries"] == "0"
    assert rows[1]["questions_completed_successfully"] == "205"
    assert b"\r\n" not in output_path.read_bytes()


def test_aggregate_runs_skips_pre_component_scorecards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    _write_scorecard(
        runs_dir / "agent-a" / "run-1" / "scorecard.json",
        agent="agent-a",
        run_id="run-1",
        overall=0.5,
        assess=0.5,
        analyze=0.5,
    )
    _write_v2_scorecard(
        runs_dir / "agent-old" / "run-old" / "scorecard.json",
        agent="agent-old",
        run_id="run-old",
    )

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 1

    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["agent"] for row in rows] == ["agent-a"]
    err = capsys.readouterr().err
    assert "Skipped 1 scorecard(s)" in err
    assert "deterministic-rules-v2" in err


def test_aggregate_runs_sorts_unscored_runs_last(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_scorecard(
        runs_dir / "agent-a" / "run-1" / "scorecard.json",
        agent="agent-a",
        run_id="run-1",
        overall=None,
        assess=None,
        analyze=None,
    )
    _write_scorecard(
        runs_dir / "agent-b" / "run-2" / "scorecard.json",
        agent="agent-b",
        run_id="run-2",
        overall=0.1,
        assess=0.1,
        analyze=0.1,
    )

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 2
    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["agent"] for row in rows] == ["agent-b", "agent-a"]


def test_aggregate_runs_rejects_malformed_scorecard(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    scorecard = runs_dir / "agent-a" / "run-1" / "scorecard.json"
    scorecard.parent.mkdir(parents=True)
    scorecard.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="could not read scorecard"):
        aggregate_runs(runs_dir, tmp_path / "scores.csv")


def test_aggregate_runs_rejects_v3_scorecard_missing_components(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    scorecard = runs_dir / "agent-a" / "run-1" / "scorecard.json"
    _write_scorecard(
        scorecard, agent="agent-a", run_id="run-1",
        overall=0.5, assess=0.5, analyze=0.5,
    )
    payload = json.loads(scorecard.read_text(encoding="utf-8"))
    del payload["overall"]["components"]["covariates"]
    scorecard.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="overall.components.covariates"):
        aggregate_runs(runs_dir, tmp_path / "scores.csv")
