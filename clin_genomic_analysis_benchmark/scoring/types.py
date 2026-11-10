"""Result types for deterministic assess scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Outcomes per component. A missing verdict (failed or invalid assess call)
# is wrong for either gold class: "fn" when gold needs clarification,
# "fp" when it does not.
OUTCOMES = ("tp", "fn", "tn", "fp")


@dataclass
class ComponentOutcome:
    component: str
    gold_needs_clarification: bool
    agent_needs_clarification: Optional[bool]  # None: no valid verdict
    outcome: str                               # one of OUTCOMES

    @property
    def correct(self) -> bool:
        return self.outcome in ("tp", "tn")


@dataclass
class AssessmentResult:
    question_id: str
    cohort: str
    gold_classification: str                   # derived from the gold verdicts
    agent_classification: Optional[str]        # derived from the agent verdicts
    components: list[ComponentOutcome]
    agent_rationales: dict[str, str] = field(default_factory=dict)
    failure_reason: Optional[str] = None
    scorer: str = "exact_component_verdicts"

    @property
    def n_correct(self) -> int:
        return sum(1 for c in self.components if c.correct)

    @property
    def classification_correct(self) -> bool:
        return self.agent_classification == self.gold_classification
