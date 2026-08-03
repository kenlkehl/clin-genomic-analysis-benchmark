"""Exact set-based scorer for the disambiguation subtask."""

from __future__ import annotations

from ..concepts import CONCEPT_IDS, validate_concept_ids
from .types import (
    DEFAULT_CORRECT_CONCEPT_POINTS,
    DEFAULT_INCORRECT_CONCEPT_PENALTY,
    ConceptDecision,
    DisambiguationScoreResult,
)


def score_disambiguation(
    *,
    question_id: str,
    cohort: str,
    gold_concept_ids: list[str],
    agent_concept_ids: list[str],
    correct_concept_points: float = DEFAULT_CORRECT_CONCEPT_POINTS,
    incorrect_concept_penalty: float = DEFAULT_INCORRECT_CONCEPT_PENALTY,
) -> DisambiguationScoreResult:
    """Score exact canonical IDs with a false-positive penalty.

    Each true-positive ID earns ``correct_concept_points``.  Each selected ID
    absent from gold subtracts ``incorrect_concept_penalty``.  Per-question
    points are floored at zero and capped at the gold maximum.
    """
    if correct_concept_points <= 0:
        raise ValueError("correct_concept_points must be > 0")
    if incorrect_concept_penalty < 0:
        raise ValueError("incorrect_concept_penalty must be >= 0")
    gold_errors = validate_concept_ids(gold_concept_ids)
    agent_errors = validate_concept_ids(agent_concept_ids)
    if gold_errors:
        raise ValueError("invalid gold concepts: " + "; ".join(gold_errors))
    if agent_errors:
        raise ValueError("invalid agent concepts: " + "; ".join(agent_errors))

    gold = set(gold_concept_ids)
    agent = set(agent_concept_ids)
    correct = gold & agent
    incorrect = agent - gold
    missed = gold - agent
    def ordered(values: set[str]) -> list[str]:
        return [concept_id for concept_id in CONCEPT_IDS if concept_id in values]
    correct_ids = ordered(correct)
    incorrect_ids = ordered(incorrect)
    missed_ids = ordered(missed)

    raw_points = (
        len(correct_ids) * correct_concept_points
        - len(incorrect_ids) * incorrect_concept_penalty
    )
    possible = len(gold_concept_ids) * correct_concept_points
    points = min(possible, max(0.0, raw_points))

    decisions: list[ConceptDecision] = []
    for concept_id in CONCEPT_IDS:
        if concept_id in correct:
            decisions.append(ConceptDecision(
                concept_id=concept_id, selected=True, gold=True,
                points=correct_concept_points, outcome="correct",
            ))
        elif concept_id in incorrect:
            decisions.append(ConceptDecision(
                concept_id=concept_id, selected=True, gold=False,
                points=-incorrect_concept_penalty, outcome="incorrect",
            ))
        elif concept_id in missed:
            decisions.append(ConceptDecision(
                concept_id=concept_id, selected=False, gold=True,
                points=0.0, outcome="missed",
            ))

    return DisambiguationScoreResult(
        question_id=question_id,
        cohort=cohort,
        n_gold=len(gold_concept_ids),
        points=points,
        decisions=decisions,
        points_per_concept=correct_concept_points,
        incorrect_concept_penalty=incorrect_concept_penalty,
        gold_concept_ids=list(gold_concept_ids),
        agent_concept_ids=list(agent_concept_ids),
        correct_concept_ids=correct_ids,
        incorrect_concept_ids=incorrect_ids,
        missed_concept_ids=missed_ids,
        raw_points_before_floor=raw_points,
    )
