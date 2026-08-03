"""Result types for deterministic disambiguation scoring."""

from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_CORRECT_CONCEPT_POINTS = 1.0
DEFAULT_INCORRECT_CONCEPT_PENALTY = 0.25


@dataclass
class ConceptDecision:
    concept_id: str
    selected: bool
    gold: bool
    points: float
    outcome: str  # "correct" | "incorrect" | "missed"


@dataclass
class DisambiguationScoreResult:
    question_id: str
    cohort: str
    n_gold: int
    points: float
    decisions: list[ConceptDecision]
    points_per_concept: float = DEFAULT_CORRECT_CONCEPT_POINTS
    incorrect_concept_penalty: float = DEFAULT_INCORRECT_CONCEPT_PENALTY
    gold_concept_ids: list[str] = field(default_factory=list)
    agent_concept_ids: list[str] = field(default_factory=list)
    correct_concept_ids: list[str] = field(default_factory=list)
    incorrect_concept_ids: list[str] = field(default_factory=list)
    missed_concept_ids: list[str] = field(default_factory=list)
    raw_points_before_floor: float = 0.0
    scorer: str = "exact_concept_ids"

    @property
    def points_possible(self) -> float:
        return self.n_gold * self.points_per_concept

    @property
    def precision(self) -> float | None:
        if not self.agent_concept_ids:
            return None
        return len(self.correct_concept_ids) / len(self.agent_concept_ids)

    @property
    def recall(self) -> float | None:
        if not self.gold_concept_ids:
            return None
        return len(self.correct_concept_ids) / len(self.gold_concept_ids)
