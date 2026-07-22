"""Render scorecards as Markdown + JSON."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .aggregator import CohortAgg, QuestionScore


def _pct(num: float, den: float) -> str:
    if den == 0:
        return "—"
    return f"{(num / den) * 100:.1f}%"


def to_markdown(*, overall: CohortAgg, per_cohort: dict[str, CohortAgg],
                question_scores: list[QuestionScore], agent_name: str, run_id: str,
                review_queue_size: int = 0) -> str:
    lines: list[str] = []
    lines.append(f"# clin-genomic-analysis-benchmark scorecard\n")
    lines.append(f"- **Agent**: `{agent_name}`")
    lines.append(f"- **Run id**: `{run_id}`")
    lines.append(f"- **Cohorts**: {len(per_cohort)}")
    lines.append(f"- **Questions scored**: {overall.n}")
    lines.append(f"- **Total points**: {overall.points:.1f} / {overall.points_possible:.1f} "
                 f"({_pct(overall.points, overall.points_possible)})")
    lines.append(f"- **Classification accuracy**: {overall.classify_accuracy * 100:.1f}%")
    if overall.mean_concept_recall is not None:
        lines.append(f"- **Mean concept recall (disambiguation)**: {overall.mean_concept_recall * 100:.1f}%")
    if overall.mean_analysis_score_norm is not None:
        lines.append(f"- **Mean analysis score (0–1)**: {overall.mean_analysis_score_norm:.3f}")
    if review_queue_size > 0:
        lines.append(f"- **Items needing manual review**: {review_queue_size}")

    lines.append("\n## Per cohort\n")
    lines.append("| cohort | n | points | / possible | % | classify acc | concept recall | analysis (0–1) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for cn in sorted(per_cohort):
        a = per_cohort[cn]
        lines.append(
            f"| {cn} | {a.n} | {a.points:.1f} | {a.points_possible:.1f} | "
            f"{_pct(a.points, a.points_possible)} | {a.classify_accuracy * 100:.1f}% | "
            f"{(a.mean_concept_recall * 100):.1f}% | "
            f"{(a.mean_analysis_score_norm if a.mean_analysis_score_norm is not None else 0):.3f} |"
            if a.mean_concept_recall is not None
            else f"| {cn} | {a.n} | {a.points:.1f} | {a.points_possible:.1f} | "
                 f"{_pct(a.points, a.points_possible)} | {a.classify_accuracy * 100:.1f}% | — | "
                 f"{(a.mean_analysis_score_norm if a.mean_analysis_score_norm is not None else 0):.3f} |"
        )

    lines.append("\n## Per category (aggregate across cohorts)\n")
    lines.append("| cat | n | points | / possible | % | classify acc | analysis bands (acc/min/maj) |")
    lines.append("|---:|---:|---:|---:|---:|---:|---|")
    for cid in sorted(overall.by_category):
        c = overall.by_category[cid]
        bands = c.analysis_band_counts
        lines.append(
            f"| {cid} | {c.n} | {c.points:.1f} | {c.points_possible:.1f} | "
            f"{_pct(c.points, c.points_possible)} | "
            f"{(c.classify_correct / c.classify_n * 100) if c.classify_n else 0:.1f}% | "
            f"{bands['accurate']}/{bands['minor']}/{bands['major']} |"
        )

    lines.append("\n## Per answer-type (analyses only)\n")
    if overall.by_answer_type:
        lines.append("| answer_type | accurate | minor | major | total |")
        lines.append("|---|---:|---:|---:|---:|")
        for atype in sorted(overall.by_answer_type):
            b = overall.by_answer_type[atype]
            tot = b["accurate"] + b["minor"] + b["major"]
            lines.append(f"| {atype} | {b['accurate']} | {b['minor']} | {b['major']} | {tot} |")
    else:
        lines.append("(no analyses attempted)")

    lines.append("\n## Per question\n")
    lines.append("| qid | cohort | cat | gold_class | agent_class | classify | disambig pts (n_covered/n_gold) | analysis pts (band) | total |")
    lines.append("|---|---|---:|---|---|---:|---|---|---:|")
    for q in sorted(question_scores, key=lambda x: (x.cohort, x.category, x.question_id)):
        agent_cls = q.classification.agent_label if q.classification else "—"
        cls_pts = q.classification.points if q.classification else 0
        if q.disambiguation:
            disamb_str = f"{q.disambiguation.points:.1f} ({q.disambiguation.n_covered}/{q.disambiguation.n_gold})"
            if q.disambiguation.n_disagreed:
                disamb_str += f" *⊘{q.disambiguation.n_disagreed} review*"
        else:
            disamb_str = "—"
        if q.analysis:
            analysis_str = f"{q.analysis.points} ({q.analysis.band.value})"
        else:
            analysis_str = f"—{('  ' + q.failure_reason) if q.failure_reason else ''}"
        lines.append(
            f"| `{q.question_id}` | {q.cohort} | {q.category} | {q.gold_classification} | "
            f"{agent_cls} | {cls_pts} | {disamb_str} | {analysis_str} | "
            f"{q.total_points:.1f}/{q.points_possible:.1f} |"
        )

    return "\n".join(lines) + "\n"


def to_json(*, overall: CohortAgg, per_cohort: dict[str, CohortAgg],
            question_scores: list[QuestionScore], agent_name: str, run_id: str) -> str:
    return json.dumps({
        "agent_name": agent_name,
        "run_id": run_id,
        "overall": _agg_to_dict(overall),
        "per_cohort": {k: _agg_to_dict(v) for k, v in per_cohort.items()},
        "questions": [_question_to_dict(q) for q in question_scores],
    }, indent=2, default=str)


def _agg_to_dict(a: CohortAgg) -> dict:
    d = asdict(a)
    # Drop private bookkeeping fields
    for cat in d.get("by_category", {}).values():
        cat.pop("_disambig_n_qs", None)
    return d


def _question_to_dict(q: QuestionScore) -> dict:
    d = asdict(q)
    if q.analysis is not None:
        d["analysis"]["band"] = q.analysis.band.value
    return d
