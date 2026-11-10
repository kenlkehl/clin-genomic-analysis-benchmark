"""Combine one scored assess run and one scored analyze run into the headline.

    SCORE = w_assess · assess + w_analyze · analyze

computed per cohort and overall. The two runs must cover the same cohorts,
have been scored against the same gold bank, and use the same conventions;
otherwise the halves measure different things and combining is refused unless
explicitly allowed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import RUNS_DIR
from ..instructions import CONVENTIONS_PATH
from ..utils.jsonio import atomic_write_text
from .driver import load_scoring_config, resolve_run_dir
from .report import SCORER, pct

SUBTASKS = ("assess", "analyze")
DEFAULT_SUBTASK_WEIGHTS: dict[str, float] = {"assess": 0.5, "analyze": 0.5}
COMBINED_DIRNAME = "combined"


def validate_subtask_weights(weights: dict[str, float]) -> dict[str, float]:
    unknown = sorted(set(weights) - set(SUBTASKS))
    if unknown:
        raise ValueError(
            f"unknown subtask weight(s): {', '.join(unknown)}; expected {', '.join(SUBTASKS)}")
    out = {name: float(weights.get(name, 0.0)) for name in SUBTASKS}
    if any(w < 0 for w in out.values()) or not any(w > 0 for w in out.values()):
        raise ValueError("subtask weights must be >= 0 with at least one > 0")
    return out


def weighted_score(scores: dict[str, Optional[float]],
                   weights: dict[str, float]) -> Optional[float]:
    """Weighted mean; a subtask with nothing to score drops out and the
    remaining weights are renormalised."""
    num = den = 0.0
    for name in SUBTASKS:
        score = scores.get(name)
        if score is None:
            continue
        num += weights[name] * score
        den += weights[name]
    return num / den if den > 0 else None


def _load_scorecard(run_path: str | Path, task: str) -> tuple[Path, dict, str]:
    run_dir = resolve_run_dir(run_path)
    path = run_dir / "scorecard.json"
    if not path.is_file():
        raise FileNotFoundError(f"{run_dir} has no scorecard.json; score it first")
    raw = path.read_bytes()
    card = json.loads(raw)
    if card.get("scorer") != SCORER or card.get("task") != task:
        raise ValueError(
            f"{path} is not a {SCORER} {task} scorecard "
            f"(scorer={card.get('scorer')!r}, task={card.get('task')!r})")
    return run_dir, card, hashlib.sha256(raw).hexdigest()


def _mismatches(assess: dict, analyze: dict) -> list[str]:
    problems: list[str] = []
    if sorted(assess.get("cohorts") or []) != sorted(analyze.get("cohorts") or []):
        problems.append(f"cohorts differ: assess {sorted(assess.get('cohorts') or [])} vs "
                        f"analyze {sorted(analyze.get('cohorts') or [])}")
    if assess.get("gold_bank_sha256") != analyze.get("gold_bank_sha256"):
        problems.append("the runs were scored against different gold banks; rescore both")
    conv = CONVENTIONS_PATH.name
    a_conv = (assess.get("rulebook") or {}).get(conv)
    b_conv = (analyze.get("rulebook") or {}).get(conv)
    if a_conv != b_conv:
        problems.append(f"the runs used different versions of {conv}")
    for card in (assess, analyze):
        for kind in ("missing_from_run", "excluded"):
            n = len((card.get("coverage") or {}).get(kind) or [])
            if n:
                problems.append(f"the {card['task']} scorecard has {n} question(s) {kind}")
    return problems


def combine_runs(
    *,
    assess_run: str | Path,
    analyze_run: str | Path,
    scoring_config_path: Optional[Path] = None,
    run_id: Optional[str] = None,
    allow_mismatch: bool = False,
) -> Path:
    """Write runs/combined/<run_id>/scorecard.{json,md}; return that directory."""
    assess_dir, assess, assess_sha = _load_scorecard(assess_run, "assess")
    analyze_dir, analyze, analyze_sha = _load_scorecard(analyze_run, "analyze")
    problems = _mismatches(assess, analyze)
    if problems and not allow_mismatch:
        raise ValueError("refusing to combine: " + "; ".join(problems))

    config = load_scoring_config(scoring_config_path)
    weights = validate_subtask_weights(config.get("subtask_weights") or DEFAULT_SUBTASK_WEIGHTS)

    def scores(cohort: Optional[str]) -> dict[str, Optional[float]]:
        a = assess["overall"] if cohort is None else (assess["per_cohort"].get(cohort) or {})
        b = analyze["overall"] if cohort is None else (analyze["per_cohort"].get(cohort) or {})
        return {"assess": a.get("score"), "analyze": b.get("score")}

    overall_scores = scores(None)
    cohorts = sorted(set(assess["per_cohort"]) | set(analyze["per_cohort"]))
    per_cohort: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        s = scores(cohort)
        per_cohort[cohort] = {"subtask_scores": s, "score": weighted_score(s, weights)}

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    out_dir = RUNS_DIR / COMBINED_DIRNAME / run_id
    if out_dir.exists():
        raise FileExistsError(f"Combined scorecard destination already exists: {out_dir}")
    out_dir.mkdir(parents=True)

    def source(run_dir: Path, card: dict, sha: str) -> dict[str, Any]:
        try:
            ref = str(run_dir.relative_to(RUNS_DIR))
        except ValueError:
            ref = str(run_dir)
        return {"run": ref, "run_name": card.get("run_name"), "run_id": card.get("run_id"),
                "scorecard_sha256": sha, "provenance": card.get("provenance") or {},
                "integrity_status": (card.get("integrity") or {}).get("status")}

    combined = {
        "scorer": SCORER,
        "task": "combined",
        "run_name": COMBINED_DIRNAME,
        "run_id": run_id,
        "sources": {"assess": source(assess_dir, assess, assess_sha),
                    "analyze": source(analyze_dir, analyze, analyze_sha)},
        "subtask_weights": weights,
        "mismatches_allowed": problems,
        "cohorts": sorted(assess.get("cohorts") or []),
        "gold_bank_sha256": assess.get("gold_bank_sha256"),
        "overall": {"subtask_scores": overall_scores,
                    "score": weighted_score(overall_scores, weights),
                    "assess": assess["overall"], "analyze": analyze["overall"]},
        "per_cohort": per_cohort,
    }
    atomic_write_text(out_dir / "scorecard.json", json.dumps(combined, indent=2, default=str))
    atomic_write_text(out_dir / "scorecard.md", _markdown(combined))
    return out_dir


def _markdown(card: dict[str, Any]) -> str:
    a, b = card["sources"]["assess"], card["sources"]["analyze"]
    w = card["subtask_weights"]
    lines = [
        "# clin-genomic-analysis-benchmark combined scorecard\n",
        f"- **SCORE: {pct(card['overall']['score'])}** — weighted mean of the two tasks",
        f"- **Assess run**: `{a['run']}` (model `{a['provenance'].get('model', '?')}`)",
        f"- **Analyze run**: `{b['run']}` (model `{b['provenance'].get('model', '?')}`)",
    ]
    if card["mismatches_allowed"]:
        lines.append("- **COMBINED DESPITE MISMATCHES**: " + "; ".join(card["mismatches_allowed"]))
    lines += [
        "",
        "| task | score | weight |",
        "|---|---:|---:|",
        f"| assess | {pct(card['overall']['subtask_scores']['assess'])} | {w['assess'] * 100:.0f}% |",
        f"| analyze | {pct(card['overall']['subtask_scores']['analyze'])} | "
        f"{w['analyze'] * 100:.0f}% |",
        "",
        "## Per cohort\n",
        "| cohort | SCORE | assess | analyze |",
        "|---|---:|---:|---:|",
    ]
    for cohort, row in card["per_cohort"].items():
        s = row["subtask_scores"]
        lines.append(f"| {cohort} | **{pct(row['score'])}** | {pct(s['assess'])} | "
                     f"{pct(s['analyze'])} |")
    lines.append("\nSee each run's own scorecard for per-component and per-question detail.")
    return "\n".join(lines) + "\n"
