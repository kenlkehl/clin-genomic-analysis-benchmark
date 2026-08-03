"""Aggregate per-question scores into per-cohort and overall summaries."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .classification import ClassificationResult
from .discrepancy import DiscrepancyResult
from .types import DisambiguationScoreResult

# The three subtasks are graded on wildly different raw scales — 1 pt per
# question for classify, 0-1 per gold concept before false-positive penalties
# for disambiguate, and 0-2 per unambiguous question for analyze — and the bank keeps changing
# (237 -> 223 -> 211 questions so far). So the headline is NOT a raw point sum.
# Each subtask is scored as a fraction of its own possible, then the three are
# combined with these weights. That keeps the balance fixed no matter how many
# concepts or questions the bank gains or loses.
#
# Note that no single question carries all three subtasks: every question is
# classify, then EITHER disambiguate (gold says ambiguous) OR analyze (gold says
# unambiguous). The weighting is therefore only meaningful in aggregate.
SUBTASKS = ("classify", "disambiguate", "analyze")
DEFAULT_SUBTASK_WEIGHTS: dict[str, float] = {
    "classify": 1 / 3,
    "disambiguate": 1 / 3,
    "analyze": 1 / 3,
}


@dataclass
class QuestionScore:
    question_id: str
    cohort: str
    category: int
    gold_classification: str               # "ambiguous" | "unambiguous"
    gold_disambiguation_n: int = 0         # gold concepts, independent of agent path
    # Max points one gold concept can earn. Needed so a question the agent never
    # reached still gets the right denominator.
    disambig_points_per_concept: float = 1.0
    classification: Optional[ClassificationResult] = None
    disambiguation: Optional[DisambiguationScoreResult] = None
    analysis: Optional[DiscrepancyResult] = None
    answer_type: Optional[str] = None      # filled when analysis attempted
    failure_reason: Optional[str] = None   # set when a stage skipped/failed

    @property
    def total_points(self) -> float:
        pts = 0.0
        if self.classification:
            pts += self.classification.points
        if self.disambiguation:
            pts += self.disambiguation.points
        if self.analysis:
            pts += self.analysis.points
        return pts

    @property
    def points_possible(self) -> float:
        # 1 (classify) + (n_gold disambig OR 2 analyze pts max)
        possible = 1.0  # classify
        if self.gold_classification == "ambiguous":
            possible += self.disambiguation_points_possible
        else:
            possible += 2.0
        return possible

    @property
    def disambiguation_points_possible(self) -> float:
        per = (self.disambiguation.points_per_concept
               if self.disambiguation is not None
               else self.disambig_points_per_concept)
        if self.gold_disambiguation_n:
            return self.gold_disambiguation_n * per
        if self.disambiguation is not None:
            return self.disambiguation.n_gold * per
        return 0.0


@dataclass
class CategoryAgg:
    category: int
    n: int = 0
    points: float = 0.0
    points_possible: float = 0.0
    classify_correct: int = 0
    classify_n: int = 0
    analysis_band_counts: dict[str, int] = field(default_factory=lambda: {"accurate": 0, "minor": 0, "major": 0})
    mean_disambiguation_score: float = 0.0  # mean penalized score across ambiguous questions
    _disambig_n_qs: int = 0


@dataclass
class CohortAgg:
    cohort: str
    n: int = 0
    points: float = 0.0                 # raw, unweighted — diagnostic only
    points_possible: float = 0.0
    classify_accuracy: float = 0.0
    mean_disambiguation_score: Optional[float] = None
    mean_analysis_score_norm: Optional[float] = None     # scaled to [0,1] over 2 max pts
    by_category: dict[int, CategoryAgg] = field(default_factory=dict)
    by_answer_type: dict[str, dict[str, int]] = field(default_factory=dict)

    # Raw points earned / available per subtask.
    subtask_points: dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in SUBTASKS})
    subtask_possible: dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in SUBTASKS})
    # Each subtask as a fraction of its own possible, and the weighted headline.
    subtask_scores: dict[str, Optional[float]] = field(
        default_factory=lambda: {k: None for k in SUBTASKS})
    subtask_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SUBTASK_WEIGHTS))
    overall_score: Optional[float] = None    # THE headline, 0-1


def _accumulate(agg: CohortAgg, q: QuestionScore) -> None:
    agg.n += 1
    agg.points += q.total_points
    agg.points_possible += q.points_possible

    # Per-subtask tallies. Denominators come from the GOLD classification, so an
    # agent that misroutes a question still owes the points it skipped.
    agg.subtask_points["classify"] += q.classification.points if q.classification else 0.0
    agg.subtask_possible["classify"] += 1.0
    if q.gold_classification == "ambiguous":
        agg.subtask_points["disambiguate"] += (
            q.disambiguation.points if q.disambiguation is not None else 0.0)
        agg.subtask_possible["disambiguate"] += q.disambiguation_points_possible
    else:
        agg.subtask_points["analyze"] += q.analysis.points if q.analysis else 0.0
        agg.subtask_possible["analyze"] += 2.0
    cat = agg.by_category.setdefault(q.category, CategoryAgg(category=q.category))
    cat.n += 1
    cat.points += q.total_points
    cat.points_possible += q.points_possible
    if q.classification is not None:
        cat.classify_n += 1
        if q.classification.correct:
            cat.classify_correct += 1
    if q.analysis is not None:
        cat.analysis_band_counts[q.analysis.band.value] += 1
        if q.answer_type:
            atype = agg.by_answer_type.setdefault(q.answer_type, {"accurate": 0, "minor": 0, "major": 0})
            atype[q.analysis.band.value] += 1
    if q.gold_classification == "ambiguous" and q.disambiguation_points_possible > 0:
        cat._disambig_n_qs += 1
        cat.mean_disambiguation_score += (
            q.disambiguation.points / q.disambiguation_points_possible
            if q.disambiguation is not None else 0.0
        )


def _finalise_cohort(agg: CohortAgg, qs: list[QuestionScore]) -> CohortAgg:
    if agg.n == 0:
        return agg
    n_classified = sum(1 for q in qs if q.classification is not None)
    n_correct = sum(1 for q in qs if q.classification is not None and q.classification.correct)
    agg.classify_accuracy = (n_correct / n_classified) if n_classified else 0.0

    disambig_questions = [
        q for q in qs
        if q.gold_classification == "ambiguous" and q.disambiguation_points_possible > 0
    ]
    if disambig_questions:
        score = sum(
            q.disambiguation.points / q.disambiguation_points_possible
            if q.disambiguation is not None else 0.0
            for q in disambig_questions
        )
        agg.mean_disambiguation_score = score / len(disambig_questions)
    analysis_questions = [q for q in qs if q.analysis is not None]
    if analysis_questions:
        # Normalise to [0, 1] (analysis max is 2 pts per question)
        agg.mean_analysis_score_norm = sum(q.analysis.points for q in analysis_questions) / (2.0 * len(analysis_questions))

    for cat in agg.by_category.values():
        if cat._disambig_n_qs > 0:
            cat.mean_disambiguation_score = (
                cat.mean_disambiguation_score / cat._disambig_n_qs)

    # Subtask fractions, then the weighted headline. A subtask with nothing to
    # score (e.g. a cohort with no ambiguous questions) drops out and the
    # remaining weights are renormalised rather than counting it as zero.
    num = den = 0.0
    for name in SUBTASKS:
        possible = agg.subtask_possible[name]
        if possible <= 0:
            agg.subtask_scores[name] = None
            continue
        frac = agg.subtask_points[name] / possible
        agg.subtask_scores[name] = frac
        w = agg.subtask_weights.get(name, 0.0)
        num += w * frac
        den += w
    agg.overall_score = (num / den) if den > 0 else None
    return agg


def aggregate(
    question_scores: list[QuestionScore],
    subtask_weights: Optional[dict[str, float]] = None,
) -> tuple[CohortAgg, dict[str, CohortAgg]]:
    """Return (overall_agg, {cohort_name: cohort_agg}).

    `subtask_weights` defaults to an equal third each; pass e.g.
    {"classify": 0.2, "disambiguate": 0.4, "analyze": 0.4} to reweight.
    """
    weights = dict(subtask_weights or DEFAULT_SUBTASK_WEIGHTS)
    overall = CohortAgg(cohort="ALL", subtask_weights=weights)
    per_cohort: dict[str, list[QuestionScore]] = defaultdict(list)
    for q in question_scores:
        per_cohort[q.cohort].append(q)
        _accumulate(overall, q)
    overall = _finalise_cohort(overall, question_scores)

    cohort_aggs: dict[str, CohortAgg] = {}
    for cname, qs in per_cohort.items():
        agg = CohortAgg(cohort=cname, subtask_weights=dict(weights))
        for q in qs:
            _accumulate(agg, q)
        cohort_aggs[cname] = _finalise_cohort(agg, qs)
    return overall, cohort_aggs
