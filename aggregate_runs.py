#!/usr/bin/env python3
"""Aggregate active benchmark scorecards into a comparison CSV.

Only scorecards at ``RUNS_DIR/<agent>/<run>/scorecard.json`` are included.
This intentionally excludes deeper archive/grouping directories such as
``runs/failed_runs`` and ``runs/contaminated_gpt_cheated``.

Assess runs, analyze runs, and combined scorecards (``runs/combined/<id>``) each
get a row, labelled by ``task``. Only scorecards from the split-task scorer
(``deterministic-rules-v4``) are aggregated. Older scorecards (a single run
holding both tasks, or classify/disambiguate and concept-ID scoring) measure a
different setup, so they are skipped with a note on stderr rather than mixed
into the comparison.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

from clin_genomic_analysis_benchmark.components import COMPONENT_IDS

SCORER = "deterministic-rules-v4"
TASKS = ("assess", "analyze", "combined")
COMPONENT_METRICS = ("balanced_accuracy", "sensitivity", "specificity")

CSV_FIELDS = (
    "task",
    "run_name",
    "run_id",
    "model",
    "provider",
    "effort",
    "integrity_status",
    "questions_scored",
    "questions_failed_after_retries",
    "questions_completed_successfully",
    "score_pct",
    "assess_score_pct",
    "analyze_score_pct",
    "question_level_accuracy_pct",
    *(
        f"{component}_{metric}_pct"
        for component in COMPONENT_IDS
        for metric in COMPONENT_METRICS
    ),
    "scorecard",
)


class IncompatibleScorecard(ValueError):
    """A well-formed scorecard from a scorer this CSV does not compare."""


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


def _questions_failed_after_retries(scorecard_path: Path) -> int:
    """Count distinct questions left unmerged by the final repair pass."""
    manifest_path = scorecard_path.with_name("manifest.json")
    if not manifest_path.is_file():
        return 0

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read manifest {manifest_path}: {exc}") from exc

    manifest = _mapping(payload, field="manifest", path=manifest_path)
    # Analyze runs record repairs in repair_history, assess runs in retry_history.
    history_field = "repair_history" if "repair_history" in manifest else "retry_history"
    repair_history = manifest.get(history_field)
    if repair_history is None:
        return 0
    if not isinstance(repair_history, list):
        raise ValueError(f"{manifest_path}: {history_field} must be a JSON array")
    if not repair_history:
        return 0

    final_repair = _mapping(
        repair_history[-1],
        field=f"{history_field}[-1]",
        path=manifest_path,
    )
    targets = final_repair.get("targets")
    if not isinstance(targets, list):
        raise ValueError(
            f"{manifest_path}: {history_field}[-1].targets must be a JSON array"
        )

    failed_questions: set[tuple[str, str]] = set()
    for index, value in enumerate(targets):
        target = _mapping(
            value,
            field=f"{history_field}[-1].targets[{index}]",
            path=manifest_path,
        )
        merged = target.get("merged")
        if not isinstance(merged, bool):
            raise ValueError(
                f"{manifest_path}: {history_field}[-1].targets[{index}].merged "
                "must be a boolean"
            )
        if not merged:
            failed_questions.add(
                (
                    _required_string(
                        target.get("cohort"),
                        field=f"{history_field}[-1].targets[{index}].cohort",
                        path=manifest_path,
                    ),
                    _required_string(
                        target.get("question_id"),
                        field=f"{history_field}[-1].targets[{index}].question_id",
                        path=manifest_path,
                    ),
                )
            )
    return len(failed_questions)


def _component_columns(assess: dict[str, Any] | None, *, field: str,
                       path: Path) -> dict[str, str]:
    if assess is None:
        return {f"{component}_{metric}_pct": "" for component in COMPONENT_IDS
                for metric in COMPONENT_METRICS}
    components = _mapping(
        assess.get("components"), field=f"{field}.components", path=path
    )
    columns: dict[str, str] = {}
    for component in COMPONENT_IDS:
        tally = _mapping(
            components.get(component),
            field=f"{field}.components.{component}",
            path=path,
        )
        for metric in COMPONENT_METRICS:
            columns[f"{component}_{metric}_pct"] = _percentage(
                tally.get(metric),
                field=f"{field}.components.{component}.{metric}",
                path=path,
            )
    return columns


def _provenance(root: dict[str, Any], task: str, *, path: Path) -> dict[str, str]:
    """Model, provider, effort, and integrity; a combined row joins both runs'."""
    if task != "combined":
        provenance = _mapping(root.get("provenance") or {}, field="provenance", path=path)
        integrity = _mapping(root.get("integrity") or {}, field="integrity", path=path)
        return {
            "model": _optional_string(provenance.get("model")),
            "provider": _optional_string(provenance.get("provider")),
            "effort": _optional_string(provenance.get("effort_level")),
            "integrity_status": _optional_string(integrity.get("status")),
        }
    sources = _mapping(root.get("sources"), field="sources", path=path)
    parts = [
        _mapping(sources.get(name), field=f"sources.{name}", path=path)
        for name in ("assess", "analyze")
    ]
    provenances = [
        _mapping(part.get("provenance") or {}, field="sources.provenance", path=path)
        for part in parts
    ]

    def joined(values: list[str]) -> str:
        return values[0] if values[0] == values[1] else " | ".join(values)

    return {
        "model": joined([_optional_string(p.get("model")) for p in provenances]),
        "provider": joined([_optional_string(p.get("provider")) for p in provenances]),
        "effort": _optional_string(provenances[1].get("effort_level")),
        "integrity_status": joined(
            [_optional_string(part.get("integrity_status")) for part in parts]),
    }


def read_scorecard(scorecard_path: Path, runs_dir: Path) -> dict[str, str | int]:
    """Read one structured scorecard and return a flat CSV row.

    Raises ``IncompatibleScorecard`` for a readable scorecard written by an
    older scorer, and ``ValueError`` for a malformed one.
    """
    try:
        payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read scorecard {scorecard_path}: {exc}") from exc

    root = _mapping(payload, field="scorecard", path=scorecard_path)
    scorer = root.get("scorer")
    if scorer != SCORER:
        raise IncompatibleScorecard(
            f"{scorecard_path}: scorer {scorer!r} is not {SCORER!r}; scorecards from "
            "before the assess/analyze split are not comparable"
        )
    task = root.get("task")
    if task not in TASKS:
        raise ValueError(f"{scorecard_path}: task must be one of {', '.join(TASKS)}")
    overall = _mapping(root.get("overall"), field="overall", path=scorecard_path)

    if task == "combined":
        subtask_scores = _mapping(overall.get("subtask_scores"),
                                  field="overall.subtask_scores", path=scorecard_path)
        assess = _mapping(overall.get("assess"), field="overall.assess", path=scorecard_path)
        assess_field = "overall.assess"
        assess_score = subtask_scores.get("assess")
        analyze_score = subtask_scores.get("analyze")
        questions_scored = _questions_scored(assess.get("n"), path=scorecard_path)
        questions_failed_after_retries = 0
    else:
        assess = overall if task == "assess" else None
        assess_field = "overall"
        assess_score = overall.get("score") if task == "assess" else None
        analyze_score = overall.get("score") if task == "analyze" else None
        questions_scored = _questions_scored(overall.get("n"), path=scorecard_path)
        questions_failed_after_retries = _questions_failed_after_retries(scorecard_path)
    if questions_failed_after_retries > questions_scored:
        raise ValueError(
            f"{scorecard_path}: questions failed after retries cannot exceed overall.n"
        )

    try:
        relative_scorecard = scorecard_path.relative_to(runs_dir)
    except ValueError:
        relative_scorecard = scorecard_path

    return {
        "task": task,
        "run_name": _required_string(
            root.get("run_name"), field="run_name", path=scorecard_path
        ),
        "run_id": _required_string(
            root.get("run_id"), field="run_id", path=scorecard_path
        ),
        **_provenance(root, task, path=scorecard_path),
        "questions_scored": questions_scored,
        "questions_failed_after_retries": questions_failed_after_retries,
        "questions_completed_successfully": (
            questions_scored - questions_failed_after_retries
        ),
        "score_pct": _percentage(
            overall.get("score"), field="overall.score", path=scorecard_path
        ),
        "assess_score_pct": _percentage(
            assess_score, field="assess score", path=scorecard_path
        ),
        "analyze_score_pct": _percentage(
            analyze_score, field="analyze score", path=scorecard_path
        ),
        "question_level_accuracy_pct": _percentage(
            assess.get("classify_accuracy") if assess is not None else None,
            field=f"{assess_field}.classify_accuracy",
            path=scorecard_path,
        ),
        **_component_columns(assess, field=assess_field, path=scorecard_path),
        "scorecard": relative_scorecard.as_posix(),
    }


def _sort_key(row: dict[str, str | int]) -> tuple[str, float, str, str]:
    score = row["score_pct"]
    return (
        str(row["task"]),
        -float(score) if score != "" else math.inf,
        str(row["run_name"]),
        str(row["run_id"]),
    )


def aggregate_runs(runs_dir: Path, output_path: Path) -> int:
    """Write active scored runs to ``output_path`` and return the row count.

    Scorecards from older scorers are skipped with a note on stderr.
    """
    if not runs_dir.is_dir():
        raise ValueError(f"runs directory does not exist: {runs_dir}")

    scorecard_paths = sorted(runs_dir.glob("*/*/scorecard.json"))
    rows: list[dict[str, str | int]] = []
    skipped: list[str] = []
    for path in scorecard_paths:
        try:
            rows.append(read_scorecard(path, runs_dir))
        except IncompatibleScorecard as exc:
            skipped.append(str(exc))
    rows.sort(key=_sort_key)
    if skipped:
        print(
            f"Skipped {len(skipped)} scorecard(s) from an older scorer:",
            file=sys.stderr,
        )
        for note in skipped:
            print(f"  - {note}", file=sys.stderr)

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
