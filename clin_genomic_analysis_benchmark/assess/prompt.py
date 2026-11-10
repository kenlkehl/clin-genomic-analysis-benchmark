"""Build the assess prompt: the rulebook, then the question."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from ..cohorts import Cohort
from ..instructions import assess_rulebook

SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class AssessPrompt:
    """One question's prompt. ``rulebook`` is identical for every question,
    so backends that support prompt caching cache it."""

    rulebook: str
    question: str

    @property
    def text(self) -> str:
        return self.rulebook.rstrip("\n") + SEPARATOR + self.question


def question_block(*, question_text: str, cohort: Cohort) -> str:
    quoted = "\n".join(f"> {line}" if line else ">" for line in question_text.strip().splitlines())
    return (
        "# Question to assess\n\n"
        f"Cohort: {cohort.disease} (`{cohort.name}`)\n\n"
        f"{quoted}\n\n"
        "Reply with only the JSON object described under \"Output\".\n"
    )


def build_assess_prompt(*, question_text: str, cohort: Cohort,
                        rulebook: Optional[str] = None) -> AssessPrompt:
    return AssessPrompt(
        rulebook=rulebook if rulebook is not None else assess_rulebook(),
        question=question_block(question_text=question_text, cohort=cohort),
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
