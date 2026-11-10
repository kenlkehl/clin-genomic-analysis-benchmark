"""Render scorecards as Markdown + JSON, one kind per task."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union

from ..components import COMPONENT_IDS, COMPONENTS_BY_ID
from .aggregator import AnalyzeAgg, AssessAgg, QuestionScore

SCORER = "deterministic-rules-v4"

# Short codes for the per-question table, e.g. "O+P" = outcome and analysis
# plan need clarification.
_CODES = {"denominator": "D", "outcome": "O", "covariates": "C", "analysis_plan": "P"}

Agg = Union[AssessAgg, AnalyzeAgg]


@dataclass
class Scorecard:
    task: str
    run_name: str
    run_id: str
    provenance: dict
    integrity: dict
    rulebook: dict
    cohorts: list[str]
    gold_bank_sha256: Optional[str]
    overall: Agg
    per_cohort: dict[str, Agg]
    question_scores: list[QuestionScore]
    excluded: list[dict[str, str]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)


def pct(value: Optional[float]) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _flag_code(flags: dict[str, Optional[bool]]) -> str:
    if any(v is None for v in flags.values()):
        return "failed"
    flagged = [_CODES[cid] for cid in COMPONENT_IDS if flags[cid]]
    return "+".join(flagged) if flagged else "none"


def provenance_lines(provenance: dict, task: str) -> list[str]:
    lines: list[str] = []
    if provenance.get("model"):
        lines.append(f"- **Model**: `{provenance['model']}`")
    if provenance.get("provider"):
        lines.append(f"- **Provider**: `{provenance['provider']}`")
    if task == "assess":
        if provenance.get("params"):
            lines.append(f"- **Request parameters**: `{json.dumps(provenance['params'])}`")
    elif provenance.get("effort_supported") is False:
        lines.append("- **Effort**: not applicable (model does not support configurable effort)")
    elif provenance.get("effort_level"):
        lines.append(f"- **Effort**: `{provenance['effort_level']}`")
    else:
        lines.append("- **Effort**: model default (not pinned)")
    return lines


def _header(lines: list[str], card: Scorecard) -> None:
    lines.append(f"# clin-genomic-analysis-benchmark {card.task} scorecard\n")
    lines.append(f"- **Task**: `{card.task}`")
    lines.append(f"- **{'Agent' if card.task == 'analyze' else 'Run name'}**: `{card.run_name}`")
    lines.append(f"- **Run id**: `{card.run_id}`")
    status = str(card.integrity.get("status") or "unaudited")
    lines.append(f"- **Integrity**: `{status}`"
                 + (" (prompt only: no tools, no data access)"
                    if card.integrity.get("boundary") == "prompt_only" else ""))
    adjudication = card.integrity.get("adjudication") or {}
    if status == "adjudicated_for_scoring":
        lines.append(
            "- **Integrity exception**: accepted for scoring under `"
            f"{adjudication.get('policy', 'unspecified')}`; original postflight findings "
            "remain in the scorecard")
    lines.extend(provenance_lines(card.provenance, card.task))
    lines.append(f"- **Cohorts**: {len(card.per_cohort)}")
    lines.append(f"- **Questions scored**: {card.overall.n}")
    if card.missing:
        lines.append(f"- **Not in this run**: {len(card.missing)} question(s) the gold bank "
                     "now expects (not scored; see the JSON scorecard)")
    if card.excluded:
        lines.append(f"- **Excluded**: {len(card.excluded)} question(s) whose gold changed "
                     "after the run (see the JSON scorecard)")
    if status == "adjudicated_for_scoring":
        lines.append("- **ACCEPTED FOR SCORING WITH A DOCUMENTED INTEGRITY EXCEPTION**")
    elif status != "valid":
        lines.append(
            "- **BENCHMARK RESULT IS NOT CERTIFIED**: filesystem-isolation "
            f"status is `{status}`")


def _assess_markdown(card: Scorecard, lines: list[str]) -> None:
    overall: AssessAgg = card.overall  # type: ignore[assignment]
    lines.append(f"- **ASSESS SCORE: {pct(overall.score)}** — mean over the four components "
                 "of balanced accuracy")
    lines.append("")
    lines.append("## Components\n")
    lines.append("Positive = needs clarification. A failed call counts as wrong for every "
                 "component.\n")
    lines.append("| component | gold unclear / n | sensitivity | specificity "
                 "| balanced acc | accuracy | no verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cid in COMPONENT_IDS:
        t = overall.components[cid]
        note = " (single-class)" if t.single_class else ""
        lines.append(
            f"| {COMPONENTS_BY_ID[cid].label} | {t.n_gold_unclear} / {t.n} | "
            f"{pct(t.sensitivity)} | {pct(t.specificity)} | "
            f"{pct(t.balanced_accuracy)}{note} | {pct(t.accuracy)} | {t.n_missing} |")
    lines.append("")
    lines.append(f"- Question-level ambiguous/unambiguous accuracy (derived from the "
                 f"verdicts; diagnostic): {pct(overall.classify_accuracy)}")

    lines.append("\n## Per cohort\n")
    lines.append("| cohort | n | assess | "
                 + " | ".join(f"{_CODES[cid]} bal acc" for cid in COMPONENT_IDS) + " |")
    lines.append("|---|---:|---:|" + "---:|" * len(COMPONENT_IDS))
    for name in sorted(card.per_cohort):
        a: AssessAgg = card.per_cohort[name]  # type: ignore[assignment]
        comps = " | ".join(pct(a.components[cid].balanced_accuracy) for cid in COMPONENT_IDS)
        lines.append(f"| {name} | {a.n} | **{pct(a.score)}** | {comps} |")

    lines.append("\n## Per category (aggregate across cohorts)\n")
    lines.append("| cat | n | verdict acc | question-level acc |")
    lines.append("|---:|---:|---:|---:|")
    for cid in sorted(overall.by_category):
        c = overall.by_category[cid]
        verdict_acc = c.verdicts_correct / c.verdicts_n if c.verdicts_n else None
        cls_acc = c.classify_correct / c.n if c.n else None
        lines.append(f"| {cid} | {c.n} | {pct(verdict_acc)} | {pct(cls_acc)} |")

    lines.append("\n## Per question\n")
    lines.append("Components needing clarification: D = denominator, O = outcome, "
                 "C = covariates, P = analysis plan.\n")
    lines.append("| qid | cohort | cat | gold | model | verdicts correct |")
    lines.append("|---|---|---:|---|---|---:|")
    for q in sorted(card.question_scores, key=lambda x: (x.cohort, x.category, x.question_id)):
        a = q.assessment
        gold = _flag_code({c.component: c.gold_needs_clarification for c in a.components})
        model = _flag_code({c.component: c.agent_needs_clarification for c in a.components})
        lines.append(f"| `{q.question_id}` | {q.cohort} | {q.category} | {gold} | {model} | "
                     f"{a.n_correct}/{len(a.components)} |")


def _analyze_markdown(card: Scorecard, lines: list[str]) -> None:
    overall: AnalyzeAgg = card.overall  # type: ignore[assignment]
    lines.append(f"- **ANALYZE SCORE: {pct(overall.score)}** — {overall.points:.1f} / "
                 f"{overall.possible:.1f} pts over gold-unambiguous questions")
    lines.append(f"- Failed or unscorable: {overall.n_failed} question(s), scored 0")
    lines.append("")

    lines.append("## Per cohort\n")
    lines.append("| cohort | n | analyze | pts | bands (acc/min/maj) | failed |")
    lines.append("|---|---:|---:|---:|---|---:|")
    for name in sorted(card.per_cohort):
        a: AnalyzeAgg = card.per_cohort[name]  # type: ignore[assignment]
        b = a.band_counts
        lines.append(f"| {name} | {a.n} | **{pct(a.score)}** | {a.points:.0f}/{a.possible:.0f} | "
                     f"{b['accurate']}/{b['minor']}/{b['major']} | {a.n_failed} |")

    lines.append("\n## Per category (aggregate across cohorts)\n")
    lines.append("| cat | n | pts | bands (acc/min/maj) |")
    lines.append("|---:|---:|---:|---|")
    for cid in sorted(overall.by_category):
        c = overall.by_category[cid]
        b = c.band_counts
        lines.append(f"| {cid} | {c.n} | {c.points:.0f}/{c.possible:.0f} | "
                     f"{b['accurate']}/{b['minor']}/{b['major']} |")

    lines.append("\n## Per answer type\n")
    if overall.by_answer_type:
        lines.append("| answer_type | accurate | minor | major | total |")
        lines.append("|---|---:|---:|---:|---:|")
        for atype in sorted(overall.by_answer_type):
            b = overall.by_answer_type[atype]
            total = b["accurate"] + b["minor"] + b["major"]
            lines.append(f"| {atype} | {b['accurate']} | {b['minor']} | {b['major']} | {total} |")
    else:
        lines.append("(no scored answers)")

    lines.append("\n## Per question\n")
    lines.append("| qid | cohort | cat | answer type | pts (band) |")
    lines.append("|---|---|---:|---|---|")
    for q in sorted(card.question_scores, key=lambda x: (x.cohort, x.category, x.question_id)):
        if q.analysis:
            result = f"{q.analysis.points} ({q.analysis.band.value})"
        else:
            result = f"0 — {q.failure_reason or 'no answer'}"
        lines.append(f"| `{q.question_id}` | {q.cohort} | {q.category} | "
                     f"{q.answer_type or '—'} | {result} |")


def to_markdown(card: Scorecard) -> str:
    lines: list[str] = []
    _header(lines, card)
    if card.task == "assess":
        _assess_markdown(card, lines)
    else:
        _analyze_markdown(card, lines)
    return "\n".join(lines) + "\n"


def _agg_to_dict(agg: Agg) -> dict:
    d = asdict(agg)
    if isinstance(agg, AssessAgg):
        d["components"] = {cid: tally.to_dict() for cid, tally in agg.components.items()}
    return d


def _question_to_dict(q: QuestionScore) -> dict:
    d = asdict(q)
    if q.analysis is not None:
        d["analysis"]["band"] = q.analysis.band.value
    if q.assessment is not None:
        d["assessment"]["n_correct"] = q.assessment.n_correct
        d["assessment"]["classification_correct"] = q.assessment.classification_correct
    return d


def to_dict(card: Scorecard) -> dict[str, Any]:
    out: dict[str, Any] = {
        "scorer": SCORER,
        "task": card.task,
        "run_name": card.run_name,
        "run_id": card.run_id,
        "provenance": card.provenance,
        "integrity": card.integrity,
        "rulebook": card.rulebook,
        "cohorts": card.cohorts,
        "gold_bank_sha256": card.gold_bank_sha256,
        "coverage": {"missing_from_run": card.missing, "excluded": card.excluded},
        "overall": _agg_to_dict(card.overall),
        "per_cohort": {k: _agg_to_dict(v) for k, v in card.per_cohort.items()},
        "questions": [_question_to_dict(q) for q in card.question_scores],
    }
    if card.task == "assess":
        out["assess_scoring"] = {
            "match": "exact_component_verdict",
            "components": list(COMPONENT_IDS),
            "score": "mean_balanced_accuracy",
            "missing_verdict": "counted_wrong",
        }
    else:
        out["analyze_scoring"] = {
            "points": "2 accurate / 1 minor / 0 major or failed",
            "score": "points over 2 per gold-unambiguous question",
        }
    return out


def to_json(card: Scorecard) -> str:
    return json.dumps(to_dict(card), indent=2, default=str)
