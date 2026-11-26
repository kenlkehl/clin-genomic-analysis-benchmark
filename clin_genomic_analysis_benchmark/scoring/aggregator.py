"""Aggregate per-question scores into per-cohort and overall summaries.

Each task is scored on its own, as a fraction in [0, 1]:

  assess  — balanced exact match. A question is right only if all four
            component verdicts match gold; a failed call is wrong. The score is
            the mean of the exact-match rates on gold-clear and gold-ambiguous
            questions, so the bank's mix of the two (about half are clear, and
            clear questions are the easy ones) does not set the score. Per-
            component balanced accuracy is kept as a diagnostic, and its mean
            (the scorer-v4 headline) is reported as the previous metric.
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
class ExactMatchTally:
    """Question-level exact matches (all four verdicts right), split by the
    gold class of the question."""
    n_clear: int = 0
    correct_clear: int = 0
    n_ambiguous: int = 0
    correct_ambiguous: int = 0

    @property
    def n(self) -> int:
        return self.n_clear + self.n_ambiguous

    @property
    def clear(self) -> Optional[float]:
        return _rate(self.correct_clear, self.n_clear)

    @property
    def ambiguous(self) -> Optional[float]:
        return _rate(self.correct_ambiguous, self.n_ambiguous)

    @property
    def overall(self) -> Optional[float]:
        return _rate(self.correct_clear + self.correct_ambiguous, self.n)

    @property
    def single_class(self) -> bool:
        """True when the slice holds only clear or only ambiguous gold questions."""
        return self.n > 0 and (self.clear is None or self.ambiguous is None)

    @property
    def balanced(self) -> Optional[float]:
        """Mean of the defined class rates; one rate alone if gold is single-class."""
        rates = [r for r in (self.clear, self.ambiguous) if r is not None]
        return sum(rates) / len(rates) if rates else None

    def add(self, *, gold_ambiguous: bool, correct: bool) -> None:
        if gold_ambiguous:
            self.n_ambiguous += 1
            self.correct_ambiguous += int(correct)
        else:
            self.n_clear += 1
            self.correct_clear += int(correct)

    def to_dict(self) -> dict:
        return {
            "n_clear": self.n_clear, "correct_clear": self.correct_clear,
            "n_ambiguous": self.n_ambiguous, "correct_ambiguous": self.correct_ambiguous,
            "clear": self.clear,
            "ambiguous": self.ambiguous,
            "overall": self.overall,
            "balanced": self.balanced,
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
    exact_match: ExactMatchTally = field(default_factory=ExactMatchTally)
    verdicts_correct: int = 0
    verdicts_n: int = 0
    classify_correct: int = 0
    score: Optional[float] = None     # balanced exact match


@dataclass
class AssessAgg:
    cohort: str
    n: int = 0
    exact_match: ExactMatchTally = field(default_factory=ExactMatchTally)
    components: dict[str, ComponentTally] = field(
        default_factory=lambda: {cid: ComponentTally() for cid in COMPONENT_IDS})
    # Question-level ambiguous/unambiguous, derived from the verdicts. Looser
    # than exact match (any unclear verdict on a gold-ambiguous question
    # counts); diagnostic.
    classify_correct: int = 0
    classify_accuracy: Optional[float] = None
    by_category: dict[int, AssessCategoryAgg] = field(default_factory=dict)
    # The scorer-v4 headline, kept so runs scored before v5 stay comparable.
    mean_component_balanced_accuracy: Optional[float] = None
    # Every component verdict pooled into one confusion table; diagnostic.
    # Its balanced accuracy weights components by how many verdicts of each
    # gold class they hold, unlike the per-component mean above.
    verdicts: ComponentTally = field(default_factory=ComponentTally)
    score: Optional[float] = None     # balanced exact match


def _accumulate_assess(agg: AssessAgg, q: QuestionScore) -> None:
    agg.n += 1
    cat = agg.by_category.setdefault(q.category, AssessCategoryAgg(category=q.category))
    cat.n += 1
    a = q.assessment
    # A question with no assessment at all is wrong, like a failed call.
    gold_ambiguous = (a.gold_classification if a else q.gold_classification) == "ambiguous"
    for tally in (agg.exact_match, cat.exact_match):
        tally.add(gold_ambiguous=gold_ambiguous, correct=a is not None and a.exact_match)
    if a is None:
        return
    for c in a.components:
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
    agg.mean_component_balanced_accuracy = sum(bal) / len(bal) if bal else None
    agg.verdicts = ComponentTally(
        **{k: sum(getattr(t, k) for t in agg.components.values())
           for k in ("tp", "fn", "tn", "fp", "n_missing")})
    agg.score = agg.exact_match.balanced
    for cat in agg.by_category.values():
        cat.score = cat.exact_match.balanced
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
