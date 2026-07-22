"""Aggregate per-question scores into per-cohort and overall summaries."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .classification import ClassificationResult
from .discrepancy import Band, DiscrepancyResult
from .judge import DisambiguationScoreResult


@dataclass
class QuestionScore:
    question_id: str
    cohort: str
    category: int
    gold_classification: str               # "ambiguous" | "unambiguous"
    gold_disambiguation_n: int = 0         # gold concepts, independent of agent path
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
            possible += float(self.disambiguation_points_possible)
        else:
            possible += 2.0
        return possible

    @property
    def disambiguation_points_possible(self) -> int:
        if self.gold_disambiguation_n:
            return self.gold_disambiguation_n
        if self.disambiguation is not None:
            return self.disambiguation.n_gold
        return 0


@dataclass
class CategoryAgg:
    category: int
    n: int = 0
    points: float = 0.0
    points_possible: float = 0.0
    classify_correct: int = 0
    classify_n: int = 0
    analysis_band_counts: dict[str, int] = field(default_factory=lambda: {"accurate": 0, "minor": 0, "major": 0})
    disambig_concept_recall: float = 0.0   # mean across ambiguous questions
    _disambig_n_qs: int = 0


@dataclass
class CohortAgg:
    cohort: str
    n: int = 0
    points: float = 0.0
    points_possible: float = 0.0
    classify_accuracy: float = 0.0
    mean_concept_recall: Optional[float] = None
    mean_analysis_score_norm: Optional[float] = None     # scaled to [0,1] over 2 max pts
    by_category: dict[int, CategoryAgg] = field(default_factory=dict)
    by_answer_type: dict[str, dict[str, int]] = field(default_factory=dict)


def _accumulate(agg: CohortAgg, q: QuestionScore) -> None:
    agg.n += 1
    agg.points += q.total_points
    agg.points_possible += q.points_possible
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
        cat.disambig_concept_recall += (
            q.disambiguation.n_covered / q.disambiguation_points_possible
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
        recall = sum(
            q.disambiguation.n_covered / q.disambiguation_points_possible
            if q.disambiguation is not None else 0.0
            for q in disambig_questions
        )
        agg.mean_concept_recall = recall / len(disambig_questions)
    analysis_questions = [q for q in qs if q.analysis is not None]
    if analysis_questions:
        # Normalise to [0, 1] (analysis max is 2 pts per question)
        agg.mean_analysis_score_norm = sum(q.analysis.points for q in analysis_questions) / (2.0 * len(analysis_questions))

    for cat in agg.by_category.values():
        if cat._disambig_n_qs > 0:
            cat.disambig_concept_recall = cat.disambig_concept_recall / cat._disambig_n_qs
    return agg


def aggregate(question_scores: list[QuestionScore]) -> tuple[CohortAgg, dict[str, CohortAgg]]:
    """Return (overall_agg, {cohort_name: cohort_agg})."""
    overall = CohortAgg(cohort="ALL")
    per_cohort: dict[str, list[QuestionScore]] = defaultdict(list)
    for q in question_scores:
        per_cohort[q.cohort].append(q)
        _accumulate(overall, q)
    overall = _finalise_cohort(overall, question_scores)

    cohort_aggs: dict[str, CohortAgg] = {}
    for cname, qs in per_cohort.items():
        agg = CohortAgg(cohort=cname)
        for q in qs:
            _accumulate(agg, q)
        cohort_aggs[cname] = _finalise_cohort(agg, qs)
    return overall, cohort_aggs
