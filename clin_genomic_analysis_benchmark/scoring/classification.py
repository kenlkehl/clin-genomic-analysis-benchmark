"""Score the binary classification subtask (ambiguous vs unambiguous)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ClassificationResult:
    correct: bool
    points: int     # 1 if correct, 0 otherwise
    agent_label: Optional[str]
    gold_label: str
    failure_reason: Optional[str] = None


def score(*, agent_label: Optional[str], gold_label: str) -> ClassificationResult:
    if agent_label not in {"ambiguous", "unambiguous"}:
        return ClassificationResult(
            correct=False, points=0, agent_label=agent_label, gold_label=gold_label,
            failure_reason=f"agent label not in {{ambiguous,unambiguous}}: {agent_label!r}",
        )
    correct = agent_label == gold_label
    return ClassificationResult(
        correct=correct, points=1 if correct else 0,
        agent_label=agent_label, gold_label=gold_label,
    )
