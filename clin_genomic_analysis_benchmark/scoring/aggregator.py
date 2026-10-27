"""Aggregate per-question scores into per-cohort and overall summaries."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..components import COMPONENT_IDS
from .discrepancy import DiscrepancyResult
from .types import AssessmentResult

# The headline is the weighted mean of two subtask scores, each a fraction in
# [0, 1]:
#
#   assess  — the mean over the four components of balanced accuracy, i.e. the
#             average of the hit rate on gold "needs clarification" verdicts and
#             on gold "clear" verdicts. Most gold verdicts are "clear", so plain
#             accuracy would reward answering "clear" everywhere; balanced
#             accuracy scores that (and its opposite) at 50%.
#   analyze — 2/1/0 points per gold-unambiguous question, over 2 points per
#             such question. An agent that flags a gold-unambiguous question
#             never reaches analyze and still owes those points.
SUBTASKS = ("assess", "analyze")
DEFAULT_SUBTASK_WEIGHTS: dict[str, float] = {"assess": 0.5, "analyze": 0.5}


def _rate(num: int, den: int) -> Optional[float]:
    return num / den if den else None


@dataclass
class ComponentTally:
    """Confusion counts for one component. Positive = needs clarification."""
    tp: int = 0
    fn: int = 0
    tn: int = 0
    fp: int = 0
    n_missing: int = 0   # no valid agent verdict; already counted in fn/fp

    @property
    def n(self) -> int:
        return self.tp + self.fn + self.tn + self.fp

    @property
    def n_gold_unclear(self) -> int:
        return self.tp + self.fn

    @property
    def sensitivity(self) -> Optional[float]:
        return _rate(self.tp, self.tp + self.fn)

    @property
    def specificity(self) -> Optional[float]:
        return _rate(self.tn, self.tn + self.fp)

    @property
    def accuracy(self) -> Optional[float]:
        return _rate(self.tp + self.tn, self.n)

    @property
    def single_class(self) -> bool:
        """True when gold has only one class here, so one rate is undefined."""
        return self.n > 0 and (self.sensitivity is None or self.specificity is None)

    @property
    def balanced_accuracy(self) -> Optional[float]:
        """Mean of the defined class rates; one rate alone if gold is single-class."""
        rates = [r for r in (self.sensitivity, self.specificity) if r is not None]
        return sum(rates) / len(rates) if rates else None

    def add(self, outcome: str, missing: bool) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)
        if missing:
            self.n_missing += 1

    def to_dict(self) -> dict:
        return {
            "tp": self.tp, "fn": self.fn, "tn": self.tn, "fp": self.fp,
            "n_missing": self.n_missing,
            "n_gold_unclear": self.n_gold_unclear,
            "sensitivity": self.sensitivity,
            "specificity": self.specificity,
            "balanced_accuracy": self.balanced_accuracy,
            "accuracy": self.accuracy,
            "single_class": self.single_class,
        }


@dataclass
class QuestionScore:
    question_id: str
    cohort: str
    category: int
    gold_classification: str               # derived from the gold verdicts
    assessment: Optional[AssessmentResult] = None
    analysis: Optional[DiscrepancyResult] = None
    answer_type: Optional[str] = None      # filled when analysis attempted
    failure_reason: Optional[str] = None   # set when a stage skipped/failed


def _band_counts() -> dict[str, int]:
    return {"accurate": 0, "minor": 0, "major": 0}


@dataclass
class CategoryAgg:
    category: int
    n: int = 0
    classify_correct: int = 0
    verdicts_correct: int = 0
    verdicts_n: int = 0
    analysis_points: float = 0.0
    analysis_possible: float = 0.0
    analysis_band_counts: dict[str, int] = field(default_factory=_band_counts)


@dataclass
class CohortAgg:
    cohort: str
    n: int = 0
    components: dict[str, ComponentTally] = field(
        default_factory=lambda: {cid: ComponentTally() for cid in COMPONENT_IDS})
    # Question-level ambiguous/unambiguous, derived from the verdicts. Not part
    # of the headline (it is implied by the component verdicts); diagnostic.
    classify_correct: int = 0
    classify_accuracy: Optional[float] = None
    analysis_points: float = 0.0
    analysis_possible: float = 0.0
    mean_analysis_score_norm: Optional[float] = None   # over analyses attempted
    by_category: dict[int, CategoryAgg] = field(default_factory=dict)
    by_answer_type: dict[str, dict[str, int]] = field(default_factory=dict)
    subtask_scores: dict[str, Optional[float]] = field(
        default_factory=lambda: {k: None for k in SUBTASKS})
    subtask_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SUBTASK_WEIGHTS))
    overall_score: Optional[float] = None    # THE headline, 0-1


def _accumulate(agg: CohortAgg, q: QuestionScore) -> None:
    agg.n += 1
    cat = agg.by_category.setdefault(q.category, CategoryAgg(category=q.category))
    cat.n += 1
    if q.assessment is not None:
        for c in q.assessment.components:
            agg.components[c.component].add(
                c.outcome, missing=c.agent_needs_clarification is None)
            cat.verdicts_n += 1
            cat.verdicts_correct += int(c.correct)
        if q.assessment.classification_correct:
            agg.classify_correct += 1
            cat.classify_correct += 1

    # Denominators come from GOLD, so a misrouted question still owes points.
    if q.gold_classification == "unambiguous":
        earned = float(q.analysis.points) if q.analysis is not None else 0.0
        agg.analysis_points += earned
        agg.analysis_possible += 2.0
        cat.analysis_points += earned
        cat.analysis_possible += 2.0
    if q.analysis is not None:
        cat.analysis_band_counts[q.analysis.band.value] += 1
        if q.answer_type:
            atype = agg.by_answer_type.setdefault(q.answer_type, _band_counts())
            atype[q.analysis.band.value] += 1


def _finalise(agg: CohortAgg, qs: list[QuestionScore]) -> CohortAgg:
    if agg.n == 0:
        return agg
    agg.classify_accuracy = agg.classify_correct / agg.n
    attempted = [q for q in qs if q.analysis is not None]
    if attempted:
        agg.mean_analysis_score_norm = (
            sum(q.analysis.points for q in attempted) / (2.0 * len(attempted)))

    bal = [t.balanced_accuracy for t in agg.components.values()
           if t.balanced_accuracy is not None]
    agg.subtask_scores["assess"] = sum(bal) / len(bal) if bal else None
    agg.subtask_scores["analyze"] = (
        agg.analysis_points / agg.analysis_possible if agg.analysis_possible else None)

    # A subtask with nothing to score (e.g. a cohort with no unambiguous
    # questions) drops out and the remaining weights are renormalised.
    num = den = 0.0
    for name in SUBTASKS:
        score = agg.subtask_scores[name]
        if score is None:
            continue
        w = agg.subtask_weights.get(name, 0.0)
        num += w * score
        den += w
    agg.overall_score = (num / den) if den > 0 else None
    return agg


def validate_subtask_weights(weights: dict[str, float]) -> dict[str, float]:
    unknown = sorted(set(weights) - set(SUBTASKS))
    if unknown:
        raise ValueError(
            f"unknown subtask weight(s): {', '.join(unknown)}; expected {', '.join(SUBTASKS)}")
    out = {name: float(weights.get(name, 0.0)) for name in SUBTASKS}
    if any(w < 0 for w in out.values()) or not any(w > 0 for w in out.values()):
        raise ValueError("subtask weights must be >= 0 with at least one > 0")
    return out


def aggregate(
    question_scores: list[QuestionScore],
    subtask_weights: Optional[dict[str, float]] = None,
) -> tuple[CohortAgg, dict[str, CohortAgg]]:
    """Return (overall_agg, {cohort_name: cohort_agg}).

    `subtask_weights` defaults to half each; pass e.g.
    {"assess": 0.6, "analyze": 0.4} to reweight.
    """
    weights = validate_subtask_weights(subtask_weights or DEFAULT_SUBTASK_WEIGHTS)
    overall = CohortAgg(cohort="ALL", subtask_weights=dict(weights))
    per_cohort: dict[str, list[QuestionScore]] = defaultdict(list)
    for q in question_scores:
        per_cohort[q.cohort].append(q)
        _accumulate(overall, q)
    overall = _finalise(overall, question_scores)

    cohort_aggs: dict[str, CohortAgg] = {}
    for cname, qs in per_cohort.items():
        agg = CohortAgg(cohort=cname, subtask_weights=dict(weights))
        for q in qs:
            _accumulate(agg, q)
        cohort_aggs[cname] = _finalise(agg, qs)
    return overall, cohort_aggs
