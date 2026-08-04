"""Render scorecards as Markdown + JSON."""

from __future__ import annotations

import json
from dataclasses import asdict

from .aggregator import SUBTASKS as _SUBTASKS
from .aggregator import CohortAgg, QuestionScore


def _pct(num: float, den: float) -> str:
    if den == 0:
        return "—"
    return f"{(num / den) * 100:.1f}%"


def to_markdown(*, overall: CohortAgg, per_cohort: dict[str, CohortAgg],
                question_scores: list[QuestionScore], agent_name: str, run_id: str,
                agent_provenance: dict | None = None,
                correct_concept_points: float = 1.0,
                incorrect_concept_penalty: float = 0.25) -> str:
    lines: list[str] = []
    lines.append("# clin-genomic-analysis-benchmark scorecard\n")
    lines.append(f"- **Agent**: `{agent_name}`")
    lines.append(f"- **Run id**: `{run_id}`")
    if agent_provenance:
        if agent_provenance.get("model"):
            lines.append(f"- **Model**: `{agent_provenance['model']}`")
        if agent_provenance.get("provider"):
            lines.append(f"- **Provider**: `{agent_provenance['provider']}`")
        if agent_provenance.get("effort_supported") is False:
            lines.append("- **Effort**: not applicable (model does not support configurable effort)")
        elif agent_provenance.get("effort_level"):
            lines.append(f"- **Effort**: `{agent_provenance['effort_level']}`")
        else:
            lines.append("- **Effort**: model default (not pinned)")
    lines.append(f"- **Cohorts**: {len(per_cohort)}")
    lines.append("- **Disambiguation scorer**: exact concept-ID match; "
                 f"+{correct_concept_points:g} per correct selection, "
                 f"−{incorrect_concept_penalty:g} per incorrect selection, floor 0")
    lines.append(f"- **Questions scored**: {overall.n}")
    if overall.overall_score is not None:
        lines.append(f"- **SCORE: {overall.overall_score * 100:.1f}%** "
                     f"— weighted mean of the three subtasks")
    lines.append("")
    lines.append("| subtask | earned | possible | score | weight |")
    lines.append("|---|---:|---:|---:|---:|")
    for name in _SUBTASKS:
        sc = overall.subtask_scores.get(name)
        lines.append(
            f"| {name} | {overall.subtask_points[name]:.1f} | "
            f"{overall.subtask_possible[name]:.1f} | "
            f"{(f'{sc * 100:.1f}%' if sc is not None else '—')} | "
            f"{overall.subtask_weights.get(name, 0) * 100:.0f}% |")
    lines.append("")
    lines.append(f"- Raw points (unweighted, diagnostic only): {overall.points:.1f} / "
                 f"{overall.points_possible:.1f} ({_pct(overall.points, overall.points_possible)})")
    lines.append(f"- **Classification accuracy**: {overall.classify_accuracy * 100:.1f}%")
    if overall.mean_disambiguation_score is not None:
        lines.append(f"- Mean disambiguation score: {overall.mean_disambiguation_score * 100:.1f}% "
                     f"_(mean of per-question fractions — unlike the subtask score "
                     f"above, every question counts equally regardless of how many "
                     f"concepts it has)_")
    if overall.mean_analysis_score_norm is not None:
        lines.append(f"- Mean analysis score (0–1): {overall.mean_analysis_score_norm:.3f} "
                     f"_(over analyses actually attempted; the subtask score above "
                     f"divides by every gold-unambiguous question, so skipping one "
                     f"costs you there but not here)_")
    lines.append("\n## Per cohort\n")
    lines.append("| cohort | n | SCORE | classify | disambiguate | analyze | raw pts |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cn in sorted(per_cohort):
        a = per_cohort[cn]
        f = lambda k: (f"{a.subtask_scores[k] * 100:.1f}%"      # noqa: E731
                       if a.subtask_scores.get(k) is not None else "—")
        lines.append(
            f"| {cn} | {a.n} | "
            f"**{(a.overall_score * 100):.1f}%** | " if a.overall_score is not None
            else f"| {cn} | {a.n} | — | ")
        lines[-1] += (f"{f('classify')} | {f('disambiguate')} | {f('analyze')} | "
                      f"{a.points:.0f}/{a.points_possible:.0f} |")

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
    lines.append("| qid | cohort | cat | gold_class | agent_class | classify | disambig pts (of possible) | analysis pts (band) | total |")
    lines.append("|---|---|---:|---|---|---:|---|---|---:|")
    for q in sorted(question_scores, key=lambda x: (x.cohort, x.category, x.question_id)):
        agent_cls = q.classification.agent_label if q.classification else "—"
        cls_pts = q.classification.points if q.classification else 0
        if q.disambiguation:
            disamb_str = (f"{q.disambiguation.points:.1f}/"
                          f"{q.disambiguation.points_possible:.0f} "
                          f"(TP={len(q.disambiguation.correct_concept_ids)}, "
                          f"FP={len(q.disambiguation.incorrect_concept_ids)}, "
                          f"FN={len(q.disambiguation.missed_concept_ids)})")
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
            question_scores: list[QuestionScore], agent_name: str, run_id: str,
            agent_provenance: dict | None = None,
            correct_concept_points: float = 1.0,
            incorrect_concept_penalty: float = 0.25) -> str:
    return json.dumps({
        "agent_name": agent_name,
        "run_id": run_id,
        "agent_provenance": agent_provenance or {},
        "scorer": "deterministic-rules-v2",
        "disambiguation_scoring": {
            "match": "exact_concept_id",
            "correct_concept_points": correct_concept_points,
            "incorrect_concept_penalty": incorrect_concept_penalty,
            "question_floor": 0.0,
        },
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
