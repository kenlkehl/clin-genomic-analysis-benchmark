#!/usr/bin/env python3
"""Aggregate active benchmark scorecards into a comparison CSV.

Only scorecards at ``RUNS_DIR/<agent>/<run>/scorecard.json`` are included.
This intentionally excludes deeper archive/grouping directories such as
``runs/failed_runs`` and ``runs/contaminated_gpt_cheated``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


CSV_FIELDS = (
    "agent",
    "run_id",
    "model",
    "provider",
    "effort",
    "integrity_status",
    "questions_scored",
    "overall_score_pct",
    "classify_score_pct",
    "disambiguate_score_pct",
    "analyze_score_pct",
    "scorecard",
)


def _mapping(value: Any, *, field: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: {field} must be a JSON object")
    return value


def _required_string(value: Any, *, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _percentage(value: Any, *, field: str, path: Path) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: {field} must be numeric or null")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{path}: {field} must be finite")
    return f"{numeric * 100:.1f}"


def _questions_scored(value: Any, *, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{path}: overall.n must be a non-negative integer")
    return value


def read_scorecard(scorecard_path: Path, runs_dir: Path) -> dict[str, str | int]:
    """Read one structured scorecard and return a flat CSV row."""
    try:
        payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read scorecard {scorecard_path}: {exc}") from exc

    root = _mapping(payload, field="scorecard", path=scorecard_path)
    overall = _mapping(root.get("overall"), field="overall", path=scorecard_path)
    subtask_scores = _mapping(
        overall.get("subtask_scores"),
        field="overall.subtask_scores",
        path=scorecard_path,
    )
    provenance = root.get("agent_provenance")
    if provenance is None:
        provenance = {}
    provenance = _mapping(
        provenance,
        field="agent_provenance",
        path=scorecard_path,
    )
    integrity = root.get("integrity")
    if integrity is None:
        integrity = {}
    integrity = _mapping(integrity, field="integrity", path=scorecard_path)

    try:
        relative_scorecard = scorecard_path.relative_to(runs_dir)
    except ValueError:
        relative_scorecard = scorecard_path

    return {
        "agent": _required_string(
            root.get("agent_name"), field="agent_name", path=scorecard_path
        ),
        "run_id": _required_string(
            root.get("run_id"), field="run_id", path=scorecard_path
        ),
        "model": _optional_string(provenance.get("model")),
        "provider": _optional_string(provenance.get("provider")),
        "effort": _optional_string(provenance.get("effort_level")),
        "integrity_status": _optional_string(integrity.get("status")),
        "questions_scored": _questions_scored(overall.get("n"), path=scorecard_path),
        "overall_score_pct": _percentage(
            overall.get("overall_score"),
            field="overall.overall_score",
            path=scorecard_path,
        ),
        "classify_score_pct": _percentage(
            subtask_scores.get("classify"),
            field="overall.subtask_scores.classify",
            path=scorecard_path,
        ),
        "disambiguate_score_pct": _percentage(
            subtask_scores.get("disambiguate"),
            field="overall.subtask_scores.disambiguate",
            path=scorecard_path,
        ),
        "analyze_score_pct": _percentage(
            subtask_scores.get("analyze"),
            field="overall.subtask_scores.analyze",
            path=scorecard_path,
        ),
        "scorecard": relative_scorecard.as_posix(),
    }


def aggregate_runs(runs_dir: Path, output_path: Path) -> int:
    """Write active scored runs to ``output_path`` and return the row count."""
    if not runs_dir.is_dir():
        raise ValueError(f"runs directory does not exist: {runs_dir}")

    scorecard_paths = sorted(runs_dir.glob("*/*/scorecard.json"))
    rows = [read_scorecard(path, runs_dir) for path in scorecard_paths]
    rows.sort(
        key=lambda row: (
            -float(row["overall_score_pct"]),
            str(row["agent"]),
            str(row["run_id"]),
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate active benchmark scorecards into a comparison CSV."
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="runs directory to scan (default: ./runs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("aggregate_runs.csv"),
        help="CSV path to write (default: ./aggregate_runs.csv)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        row_count = aggregate_runs(args.runs_dir, args.output)
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    print(f"Wrote {row_count} scored run(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
