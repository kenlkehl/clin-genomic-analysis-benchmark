"""Aggregate per-question scores into per-cohort and overall summaries.

Each task is scored on its own, as a fraction in [0, 1]:

  assess  — the mean over the four components of balanced accuracy, i.e. the
            average of the hit rate on gold "needs clarification" verdicts and
            on gold "clear" verdicts. Most gold verdicts are "clear", so plain
            accuracy would reward answering "clear" everywhere; balanced
            accuracy scores that (and its opposite) at 50%.
  analyze — 2/1/0 points per gold-unambiguous question, over 2 points per
            question. Every such question is attempted; a failed one scores 0.

``combine`` weights one assess run and one analyze run into the headline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..components import COMPONENT_IDS
from .discrepancy import DiscrepancyResult
from .types import AssessmentResult


def _rate(num: int, den: int) -> Optional[float]:
    return num / den if den else None


@dataclass
class ComponentTally:
    """Confusion counts for one component. Positive = needs clarification."""
    tp: int = 0
    fn: int = 0
    tn: int = 0
    fp: int = 0
    n_missing: int = 0   # no valid verdict; already counted in fn/fp

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
    gold_classification: str
    assessment: Optional[AssessmentResult] = None   # assess runs
    analysis: Optional[DiscrepancyResult] = None    # analyze runs
    answer_type: Optional[str] = None
    failure_reason: Optional[str] = None


def _band_counts() -> dict[str, int]:
    return {"accurate": 0, "minor": 0, "major": 0}


# ---- assess -------------------------------------------------------------------

@dataclass
class AssessCategoryAgg:
    category: int
    n: int = 0
    verdicts_correct: int = 0
    verdicts_n: int = 0
    classify_correct: int = 0


@dataclass
class AssessAgg:
    cohort: str
    n: int = 0
    components: dict[str, ComponentTally] = field(
        default_factory=lambda: {cid: ComponentTally() for cid in COMPONENT_IDS})
    # Question-level ambiguous/unambiguous, derived from the verdicts. Not part
    # of the score (it is implied by the component verdicts); diagnostic.
    classify_correct: int = 0
    classify_accuracy: Optional[float] = None
    by_category: dict[int, AssessCategoryAgg] = field(default_factory=dict)
    score: Optional[float] = None


def _accumulate_assess(agg: AssessAgg, q: QuestionScore) -> None:
    agg.n += 1
    cat = agg.by_category.setdefault(q.category, AssessCategoryAgg(category=q.category))
    cat.n += 1
    if q.assessment is None:
        return
    for c in q.assessment.components:
        agg.components[c.component].add(c.outcome, missing=c.agent_needs_clarification is None)
        cat.verdicts_n += 1
        cat.verdicts_correct += int(c.correct)
    if q.assessment.classification_correct:
        agg.classify_correct += 1
        cat.classify_correct += 1


def _finalise_assess(agg: AssessAgg) -> AssessAgg:
    if agg.n:
        agg.classify_accuracy = agg.classify_correct / agg.n
    bal = [t.balanced_accuracy for t in agg.components.values()
           if t.balanced_accuracy is not None]
    agg.score = sum(bal) / len(bal) if bal else None
    return agg


def aggregate_assess(
    question_scores: list[QuestionScore],
) -> tuple[AssessAgg, dict[str, AssessAgg]]:
    """Return (overall, {cohort: agg}) for an assess run."""
    overall = AssessAgg(cohort="ALL")
    by_cohort: dict[str, AssessAgg] = defaultdict(lambda: AssessAgg(cohort=""))
    for q in question_scores:
        _accumulate_assess(overall, q)
        agg = by_cohort[q.cohort]
        agg.cohort = q.cohort
        _accumulate_assess(agg, q)
    return _finalise_assess(overall), {k: _finalise_assess(v) for k, v in by_cohort.items()}


# ---- analyze --------------------------------------------------------------------

@dataclass
class AnalyzeCategoryAgg:
    category: int
    n: int = 0
    points: float = 0.0
    possible: float = 0.0
    band_counts: dict[str, int] = field(default_factory=_band_counts)


@dataclass
class AnalyzeAgg:
    cohort: str
    n: int = 0
    n_failed: int = 0      # no scored answer (agent failure or missing gold answer)
    points: float = 0.0
    possible: float = 0.0
    band_counts: dict[str, int] = field(default_factory=_band_counts)
    by_category: dict[int, AnalyzeCategoryAgg] = field(default_factory=dict)
    by_answer_type: dict[str, dict[str, int]] = field(default_factory=dict)
    score: Optional[float] = None


def _accumulate_analyze(agg: AnalyzeAgg, q: QuestionScore) -> None:
    agg.n += 1
    cat = agg.by_category.setdefault(q.category, AnalyzeCategoryAgg(category=q.category))
    cat.n += 1
    earned = float(q.analysis.points) if q.analysis is not None else 0.0
    agg.points += earned
    agg.possible += 2.0
    cat.points += earned
    cat.possible += 2.0
    if q.analysis is None:
        agg.n_failed += 1
        return
    band = q.analysis.band.value
    agg.band_counts[band] += 1
    cat.band_counts[band] += 1
    if q.answer_type:
        agg.by_answer_type.setdefault(q.answer_type, _band_counts())[band] += 1


def _finalise_analyze(agg: AnalyzeAgg) -> AnalyzeAgg:
    agg.score = agg.points / agg.possible if agg.possible else None
    return agg


def aggregate_analyze(
    question_scores: list[QuestionScore],
) -> tuple[AnalyzeAgg, dict[str, AnalyzeAgg]]:
    """Return (overall, {cohort: agg}) for an analyze run."""
    overall = AnalyzeAgg(cohort="ALL")
    by_cohort: dict[str, AnalyzeAgg] = defaultdict(lambda: AnalyzeAgg(cohort=""))
    for q in question_scores:
        _accumulate_analyze(overall, q)
        agg = by_cohort[q.cohort]
        agg.cohort = q.cohort
        _accumulate_analyze(agg, q)
    return _finalise_analyze(overall), {k: _finalise_analyze(v) for k, v in by_cohort.items()}
