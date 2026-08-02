"""Result types for the disambiguation subtask.

Two independent LLM judges each answer, per gold concept, "does the agent's list
address this core concept at all?" as yes / no / unable to determine, worth
2 / 0 / 1 points. A concept's score is the sum across both judges, so it runs
0-4. Nothing needs a human: the judges are never required to agree, and a split
simply lands in the middle of the scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Per-judge verdicts and what each is worth.
LABEL_POINTS: dict[str, float] = {
    "yes": 2.0,
    "unable to determine": 1.0,
    "no": 0.0,
}
JUDGE_MAX_PER_CONCEPT = 4.0     # two judges x 2 points


@dataclass
class ConceptDecision:
    gold_concept: str
    points: float
    # e.g. {"claude": "yes", "azure": "no"}
    labels: dict[str, str] = field(default_factory=dict)
    reasoning: dict[str, str] = field(default_factory=dict)
    explanation: str = ""

    @property
    def summary(self) -> str:
        if self.labels:
            return " / ".join(f"{k}:{v}" for k, v in sorted(self.labels.items()))
        return self.explanation


@dataclass
class DisambiguationScoreResult:
    question_id: str
    cohort: str
    n_gold: int
    points: float
    decisions: list[ConceptDecision]
    points_per_concept: float = JUDGE_MAX_PER_CONCEPT
    question_text: str = ""
    agent_concepts: list[str] = field(default_factory=list)
    scorer: str = "llm_judge"
    # Judges that returned no usable verdict for a concept — scored as "unable
    # to determine" and surfaced so a flaky endpoint cannot pass for a hard call.
    n_missing_verdicts: int = 0

    @property
    def points_possible(self) -> float:
        return self.n_gold * self.points_per_concept

    @property
    def n_unable(self) -> int:
        return sum(1 for d in self.decisions
                   for v in d.labels.values() if v == "unable to determine")

    @property
    def n_split(self) -> int:
        """Concepts the two judges scored differently. Informational only."""
        return sum(1 for d in self.decisions if len(set(d.labels.values())) > 1)


def label_points(label: Optional[str]) -> float:
    """Points for one judge's verdict; an unusable verdict counts as 'unable'."""
    if label is None:
        return LABEL_POINTS["unable to determine"]
    return LABEL_POINTS.get(label.strip().lower(), LABEL_POINTS["unable to determine"])
