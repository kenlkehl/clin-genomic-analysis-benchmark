"""Render scorecards as Markdown + JSON."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from ..components import COMPONENT_IDS, COMPONENTS_BY_ID
from .aggregator import SUBTASKS as _SUBTASKS
from .aggregator import CohortAgg, QuestionScore

# Short codes for the per-question table, e.g. "O+P" = outcome and analysis
# plan need clarification.
_CODES = {"denominator": "D", "outcome": "O", "covariates": "C", "analysis_plan": "P"}


def _pct(value: Optional[float]) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _flag_code(flags: dict[str, Optional[bool]]) -> str:
    if any(v is None for v in flags.values()):
        return "failed"
    flagged = [_CODES[cid] for cid in COMPONENT_IDS if flags[cid]]
    return "+".join(flagged) if flagged else "none"


def _header(lines: list[str], *, agent_name: str, run_id: str, n_cohorts: int,
            n_questions: int, agent_provenance: dict | None,
            integrity: dict | None) -> None:
    lines.append("# clin-genomic-analysis-benchmark scorecard\n")
    lines.append(f"- **Agent**: `{agent_name}`")
    lines.append(f"- **Run id**: `{run_id}`")
    integrity_status = str((integrity or {}).get("status") or "unaudited")
    lines.append(f"- **Integrity**: `{integrity_status}`")
    adjudication = (integrity or {}).get("adjudication") or {}
    if integrity_status == "adjudicated_for_scoring":
        lines.append(
            "- **Integrity exception**: accepted for scoring under `"
            f"{adjudication.get('policy', 'unspecified')}`; original postflight findings "
            "remain in the scorecard"
        )
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
    lines.append(f"- **Cohorts**: {n_cohorts}")
    lines.append("- **Assess scorer**: exact per-component verdicts; subtask score = mean "
                 "balanced accuracy over the four components")
    lines.append(f"- **Questions scored**: {n_questions}")
    if integrity_status == "adjudicated_for_scoring":
        lines.append("- **ACCEPTED FOR SCORING WITH A DOCUMENTED INTEGRITY EXCEPTION**")
    elif integrity_status != "valid":
        lines.append(
            "- **BENCHMARK RESULT IS NOT CERTIFIED**: filesystem-isolation "
            f"status is `{integrity_status}`"
        )


def to_markdown(*, overall: CohortAgg, per_cohort: dict[str, CohortAgg],
                question_scores: list[QuestionScore], agent_name: str, run_id: str,
                agent_provenance: dict | None = None,
                integrity: dict | None = None) -> str:
    lines: list[str] = []
    _header(lines, agent_name=agent_name, run_id=run_id, n_cohorts=len(per_cohort),
            n_questions=overall.n, agent_provenance=agent_provenance, integrity=integrity)
    if overall.overall_score is not None:
        lines.append(f"- **SCORE: {overall.overall_score * 100:.1f}%** "
                     f"— weighted mean of the two subtasks")
    lines.append("")
    lines.append("| subtask | score | weight | basis |")
    lines.append("|---|---:|---:|---|")
    for name in _SUBTASKS:
        if name == "assess":
            basis = "mean of per-component balanced accuracy"
        else:
            basis = (f"{overall.analysis_points:.1f} / {overall.analysis_possible:.1f} pts "
                     "over gold-unambiguous questions")
        lines.append(
            f"| {name} | {_pct(overall.subtask_scores.get(name))} | "
            f"{overall.subtask_weights.get(name, 0) * 100:.0f}% | {basis} |")
    lines.append("")
    lines.append("## Components\n")
    lines.append("Positive = needs clarification. A failed assess stage counts as wrong "
                 "for every component.\n")
    lines.append("| component | gold unclear / n | sensitivity | specificity "
                 "| balanced acc | accuracy | no verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cid in COMPONENT_IDS:
        t = overall.components[cid]
        note = " (single-class)" if t.single_class else ""
        lines.append(
            f"| {COMPONENTS_BY_ID[cid].label} | {t.n_gold_unclear} / {t.n} | "
            f"{_pct(t.sensitivity)} | {_pct(t.specificity)} | "
            f"{_pct(t.balanced_accuracy)}{note} | {_pct(t.accuracy)} | {t.n_missing} |")
    lines.append("")
    lines.append(f"- Question-level ambiguous/unambiguous accuracy (derived from the "
                 f"verdicts; diagnostic): {_pct(overall.classify_accuracy)}")
    if overall.mean_analysis_score_norm is not None:
        lines.append(f"- Mean analysis score (0–1): {overall.mean_analysis_score_norm:.3f} "
                     f"_(over analyses actually attempted; the subtask score above "
                     f"divides by every gold-unambiguous question, so skipping one "
                     f"costs you there but not here)_")

    lines.append("\n## Per cohort\n")
    lines.append("| cohort | n | SCORE | assess | analyze | "
                 + " | ".join(f"{_CODES[cid]} bal acc" for cid in COMPONENT_IDS) + " |")
    lines.append("|---|---:|---:|---:|---:|" + "---:|" * len(COMPONENT_IDS))
    for cn in sorted(per_cohort):
        a = per_cohort[cn]
        comps = " | ".join(_pct(a.components[cid].balanced_accuracy)
                           for cid in COMPONENT_IDS)
        lines.append(
            f"| {cn} | {a.n} | **{_pct(a.overall_score)}** | "
            f"{_pct(a.subtask_scores.get('assess'))} | "
            f"{_pct(a.subtask_scores.get('analyze'))} | {comps} |")

    lines.append("\n## Per category (aggregate across cohorts)\n")
    lines.append("| cat | n | verdict acc | question-level acc | analysis pts | "
                 "analysis bands (acc/min/maj) |")
    lines.append("|---:|---:|---:|---:|---:|---|")
    for cid in sorted(overall.by_category):
        c = overall.by_category[cid]
        bands = c.analysis_band_counts
        verdict_acc = c.verdicts_correct / c.verdicts_n if c.verdicts_n else None
        cls_acc = c.classify_correct / c.n if c.n else None
        lines.append(
            f"| {cid} | {c.n} | {_pct(verdict_acc)} | {_pct(cls_acc)} | "
            f"{c.analysis_points:.0f}/{c.analysis_possible:.0f} | "
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
    lines.append("Components needing clarification: D = denominator, O = outcome, "
                 "C = covariates, P = analysis plan.\n")
    lines.append("| qid | cohort | cat | gold | agent | verdicts correct | analysis pts (band) |")
    lines.append("|---|---|---:|---|---|---:|---|")
    for q in sorted(question_scores, key=lambda x: (x.cohort, x.category, x.question_id)):
        a = q.assessment
        if a is not None:
            gold = _flag_code({c.component: c.gold_needs_clarification for c in a.components})
            agent = _flag_code({c.component: c.agent_needs_clarification
                                for c in a.components})
            correct = f"{a.n_correct}/{len(a.components)}"
        else:
            gold = agent = correct = "—"
        if q.analysis:
            analysis_str = f"{q.analysis.points} ({q.analysis.band.value})"
        else:
            analysis_str = f"—{('  ' + q.failure_reason) if q.failure_reason else ''}"
        lines.append(
            f"| `{q.question_id}` | {q.cohort} | {q.category} | {gold} | {agent} | "
            f"{correct} | {analysis_str} |"
        )

    return "\n".join(lines) + "\n"


def to_json(*, overall: CohortAgg, per_cohort: dict[str, CohortAgg],
            question_scores: list[QuestionScore], agent_name: str, run_id: str,
            agent_provenance: dict | None = None,
            integrity: dict | None = None) -> str:
    return json.dumps({
        "agent_name": agent_name,
        "run_id": run_id,
        "agent_provenance": agent_provenance or {},
        "integrity": integrity or {"status": "unaudited"},
        "scorer": "deterministic-rules-v3",
        "assess_scoring": {
            "match": "exact_component_verdict",
            "components": list(COMPONENT_IDS),
            "subtask_score": "mean_balanced_accuracy",
            "missing_verdict": "counted_wrong",
        },
        "overall": _agg_to_dict(overall),
        "per_cohort": {k: _agg_to_dict(v) for k, v in per_cohort.items()},
        "questions": [_question_to_dict(q) for q in question_scores],
    }, indent=2, default=str)


def _agg_to_dict(a: CohortAgg) -> dict:
    d = asdict(a)
    d["components"] = {cid: tally.to_dict() for cid, tally in a.components.items()}
    return d


def _question_to_dict(q: QuestionScore) -> dict:
    d = asdict(q)
    if q.analysis is not None:
        d["analysis"]["band"] = q.analysis.band.value
    if q.assessment is not None:
        d["assessment"]["n_correct"] = q.assessment.n_correct
        d["assessment"]["classification_correct"] = q.assessment.classification_correct
    return d
