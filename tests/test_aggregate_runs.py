from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from aggregate_runs import aggregate_runs


def _write_scorecard(
    path: Path,
    *,
    agent: str,
    run_id: str,
    overall: float,
    classify: float,
    disambiguate: float | None,
    analyze: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_name": agent,
                "run_id": run_id,
                "agent_provenance": {
                    "model": "test-model",
                    "provider": "test-provider",
                    "effort_level": "high",
                },
                "integrity": {"status": "valid"},
                "overall": {
                    "n": 211,
                    "overall_score": overall,
                    "subtask_scores": {
                        "classify": classify,
                        "disambiguate": disambiguate,
                        "analyze": analyze,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_aggregate_runs_includes_only_active_two_level_scorecards(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _write_scorecard(
        runs_dir / "agent-a" / "run-1" / "scorecard.json",
        agent="agent-a",
        run_id="run-1",
        overall=0.6254,
        classify=0.75,
        disambiguate=None,
        analyze=0.5,
    )
    _write_scorecard(
        runs_dir / "agent-b" / "run-2" / "scorecard.json",
        agent="agent-b",
        run_id="run-2",
        overall=0.8,
        classify=0.9,
        disambiguate=0.7,
        analyze=0.8,
    )
    _write_scorecard(
        runs_dir / "failed_runs" / "agent-c" / "run-3" / "scorecard.json",
        agent="agent-c",
        run_id="run-3",
        overall=1.0,
        classify=1.0,
        disambiguate=1.0,
        analyze=1.0,
    )
    _write_scorecard(
        runs_dir / "agent-a" / "pre_repair" / "raw-run" / "scorecard.json",
        agent="agent-a",
        run_id="raw-run",
        overall=1.0,
        classify=1.0,
        disambiguate=1.0,
        analyze=1.0,
    )

    output_path = tmp_path / "scores.csv"
    assert aggregate_runs(runs_dir, output_path) == 2

    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["agent"] for row in rows] == ["agent-b", "agent-a"]
    assert rows[0] == {
        "agent": "agent-b",
        "run_id": "run-2",
        "model": "test-model",
        "provider": "test-provider",
        "effort": "high",
        "integrity_status": "valid",
        "questions_scored": "211",
        "overall_score_pct": "80.0",
        "classify_score_pct": "90.0",
        "disambiguate_score_pct": "70.0",
        "analyze_score_pct": "80.0",
        "scorecard": "agent-b/run-2/scorecard.json",
    }
    assert rows[1]["overall_score_pct"] == "62.5"
    assert rows[1]["disambiguate_score_pct"] == ""
    assert b"\r\n" not in output_path.read_bytes()


def test_aggregate_runs_rejects_malformed_scorecard(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    scorecard = runs_dir / "agent-a" / "run-1" / "scorecard.json"
    scorecard.parent.mkdir(parents=True)
    scorecard.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="could not read scorecard"):
        aggregate_runs(runs_dir, tmp_path / "scores.csv")
