"""Dual-model gold-script reviewer (Claude/Vertex + Azure OpenAI).

Reviewers see the question, the analysis spec, the cohort file inventory, the
required answer-type schema, and the candidate Python script. They DO NOT see
patient rows.

Outcomes:
  - Both approve  → ReviewOutcome.APPROVED
  - Both reject   → ReviewOutcome.REJECTED   (issues = union of both reviewers')
  - Disagreement  → ReviewOutcome.DISAGREEMENT (write to gold_standard_review_queue.yaml,
                                               do NOT execute the script as canonical)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from jinja2 import Template
from pydantic import BaseModel, ConfigDict, ValidationError

from ..cohorts import Cohort, categorize_files
from ..config import PROMPTS_DIR, gold_standard_dir
from ..llm.azure_openai_client import AzureClient
from ..llm.vertex_client import VertexClient
from ..utils.jsonio import atomic_write_text, extract_json
from .answer_types import ANSWER_TYPES

logger = logging.getLogger(__name__)


class _ReviewerVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approve: bool
    issues: list[str] = []
    suggested_fix: str = ""


@dataclass
class ReviewerOutput:
    name: str                       # "claude_vertex" | "azure_openai"
    approve: bool
    issues: list[str]
    suggested_fix: str
    raw: str


class ReviewOutcome(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    DISAGREEMENT = "disagreement"


@dataclass
class ReviewResult:
    outcome: ReviewOutcome
    reviewers: list[ReviewerOutput] = field(default_factory=list)

    @property
    def union_issues(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for r in self.reviewers:
            for issue in r.issues:
                if issue not in seen:
                    seen.add(issue)
                    out.append(issue)
        return out

    @property
    def combined_suggested_fix(self) -> str:
        bits = [r.suggested_fix for r in self.reviewers if r.suggested_fix.strip()]
        return "\n\n---\n\n".join(bits)


_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "gold_review_system.md"
_USER_PROMPT_PATH = PROMPTS_DIR / "gold_review_user.md.j2"


def _answer_schema_md(answer_type: str) -> str:
    spec = ANSWER_TYPES.get(answer_type)
    if spec is None:
        return f"(unknown answer type: {answer_type})"
    return (
        f"- required: {', '.join(spec.required_fields)}\n"
        f"- optional: {', '.join(spec.optional_fields) if spec.optional_fields else '(none)'}"
    )


def _render_user_prompt(*, cohort: Cohort, question, script: str) -> str:
    user_tmpl = Template(_USER_PROMPT_PATH.read_text())
    files_by_category = {k: [p.name for p in v] for k, v in categorize_files(cohort).items() if v}
    return user_tmpl.render(
        cohort=cohort,
        question=question,
        script=script,
        files_by_category=files_by_category,
        answer_schema_md=_answer_schema_md(question.analysis_spec.expected_answer_type),
    )


def _parse_verdict(text: str, reviewer_name: str) -> ReviewerOutput:
    parsed = extract_json(text)
    if not isinstance(parsed, dict):
        logger.warning("[%s] reviewer returned non-JSON; defaulting to reject. Raw: %s",
                       reviewer_name, text[:500])
        return ReviewerOutput(name=reviewer_name, approve=False,
                              issues=["reviewer returned non-JSON"],
                              suggested_fix="", raw=text)
    try:
        verdict = _ReviewerVerdict.model_validate(parsed)
    except ValidationError as e:
        logger.warning("[%s] reviewer JSON failed schema: %s", reviewer_name, e)
        return ReviewerOutput(name=reviewer_name, approve=False,
                              issues=[f"reviewer schema violation: {e}"],
                              suggested_fix="", raw=text)
    return ReviewerOutput(
        name=reviewer_name,
        approve=verdict.approve,
        issues=list(verdict.issues),
        suggested_fix=verdict.suggested_fix or "",
        raw=text,
    )


def review(
    *,
    cohort: Cohort,
    question,                       # questions.schema.Question
    script: str,
    claude_client: Optional[VertexClient] = None,
    azure_client: Optional[AzureClient] = None,
) -> ReviewResult:
    """Run both reviewers and synthesise an outcome."""
    if question.analysis_spec is None:
        raise ValueError("review() called on a question without analysis_spec")

    system_prompt = _SYSTEM_PROMPT_PATH.read_text()
    user_prompt = _render_user_prompt(cohort=cohort, question=question, script=script)

    claude_client = claude_client or VertexClient.from_env()
    azure_client = azure_client or AzureClient.from_env()

    # Reviewer 1: Claude (Vertex)
    claude_resp = claude_client.generate(
        system_text=system_prompt,
        user_text=user_prompt,
        max_tokens=2000,
    )
    claude_verdict = _parse_verdict(claude_resp.text, "claude_vertex")

    # Reviewer 2: Azure OpenAI
    # gpt-5 burns most of max_tokens on internal reasoning before emitting visible
    # output, so 2000 leaves the response empty. 12000 yields ~4-8k of visible
    # budget after reasoning, which is plenty for a JSON verdict.
    azure_resp = azure_client.generate(
        system_text=system_prompt,
        user_text=user_prompt,
        max_tokens=12000,
    )
    azure_verdict = _parse_verdict(azure_resp.text, "azure_openai")

    reviewers = [claude_verdict, azure_verdict]
    if claude_verdict.approve and azure_verdict.approve:
        outcome = ReviewOutcome.APPROVED
    elif (not claude_verdict.approve) and (not azure_verdict.approve):
        outcome = ReviewOutcome.REJECTED
    else:
        outcome = ReviewOutcome.DISAGREEMENT
    return ReviewResult(outcome=outcome, reviewers=reviewers)


# ---- Disagreement queue ----

def review_queue_path() -> Path:
    return gold_standard_dir() / "gold_standard_review_queue.yaml"


def append_disagreement(*, cohort: Cohort, question, script: str, result: ReviewResult,
                        attempt: int) -> Path:
    """Append a disagreement entry to the cross-cohort review queue YAML."""
    queue_path = review_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if queue_path.exists():
        existing = yaml.safe_load(queue_path.read_text()) or []
    entry = {
        "cohort": cohort.name,
        "question_id": question.id,
        "question_text": question.text,
        "attempt": attempt,
        "reviewers": [
            {"name": r.name, "approve": r.approve, "issues": r.issues,
             "suggested_fix": r.suggested_fix}
            for r in result.reviewers
        ],
        "script_excerpt": script[:2000],
    }
    existing.append(entry)
    atomic_write_text(queue_path, yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
    return queue_path
