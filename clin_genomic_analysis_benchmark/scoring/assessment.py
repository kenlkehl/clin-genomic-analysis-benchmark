"""Exact per-component scorer for the assess task."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from ..components import COMPONENT_IDS, classification_from_flags
from .types import AssessmentResult, ComponentOutcome


def _outcome(gold: bool, agent: Optional[bool]) -> str:
    if gold:
        return "tp" if agent is True else "fn"
    return "tn" if agent is False else "fp"


def score_assessment(
    *,
    question_id: str,
    cohort: str,
    gold_flags: Mapping[str, bool],
    agent_flags: Optional[Mapping[str, bool]],
    agent_rationales: Optional[Mapping[str, str]] = None,
    failure_reason: Optional[str] = None,
) -> AssessmentResult:
    """Compare the agent's ``needs_clarification`` verdicts with gold.

    ``agent_flags`` is None when the assess call failed; every component then
    counts as wrong. Rationales are recorded but never scored.
    """
    gold_classification = classification_from_flags(gold_flags)
    agent_classification = (
        classification_from_flags(agent_flags) if agent_flags is not None else None)
    components = [
        ComponentOutcome(
            component=cid,
            gold_needs_clarification=bool(gold_flags[cid]),
            agent_needs_clarification=(
                agent_flags[cid] if agent_flags is not None else None),
            outcome=_outcome(
                bool(gold_flags[cid]),
                agent_flags[cid] if agent_flags is not None else None),
        )
        for cid in COMPONENT_IDS
    ]
    return AssessmentResult(
        question_id=question_id,
        cohort=cohort,
        gold_classification=gold_classification,
        agent_classification=agent_classification,
        components=components,
        agent_rationales=dict(agent_rationales or {}),
        failure_reason=failure_reason,
    )
