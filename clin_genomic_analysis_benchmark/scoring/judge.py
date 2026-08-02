"""Two-LLM judge for the disambiguation subtask.

Each judge is asked one plain question per gold concept — *does the agent's list
address this core concept at all?* — and answers yes, no, or unable to determine,
worth 2, 0, or 1 point. A concept's score is the sum of both judges' points, so
it runs 0-4.

The judges are never required to agree and there is no tie-break. A split lands
at 2/4 on its own, which is the honest reading of a genuinely borderline answer,
and no human is ever in the loop.

Both judges see the same prompt (`prompts/judge_disambiguation_*`), which asks
for reasoning before the verdict and forbids one vague agent item from
collecting credit across several concepts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import PROMPTS_DIR
from ..llm.azure_openai_client import AzureClient
from ..llm.vertex_client import VertexClient
from ..utils.jsonio import extract_json
from .types import (
    JUDGE_MAX_PER_CONCEPT,
    ConceptDecision,
    DisambiguationScoreResult,
    label_points,
)

logger = logging.getLogger(__name__)

__all__ = ["score_disambiguation", "JUDGE_NAMES"]

JUDGE_NAMES = ("claude", "azure")

_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "judge_disambiguation_system.md"
_USER_PROMPT_PATH = PROMPTS_DIR / "judge_disambiguation_user.md.j2"

_VALID = {"yes", "no", "unable to determine"}


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gold_concept: str = ""
    reasoning: str = ""
    answer: str = ""


class _VerdictList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdicts: list[_Verdict]


def _normalise_answer(raw: str) -> Optional[str]:
    """Map a judge's answer onto yes / no / unable to determine."""
    a = (raw or "").strip().lower().rstrip(".")
    if a in _VALID:
        return a
    if a in {"y", "true", "covered", "addressed"}:
        return "yes"
    if a in {"n", "false", "not covered", "not addressed"}:
        return "no"
    if a.startswith("unable") or a in {"unclear", "uncertain", "unknown", "cannot determine"}:
        return "unable to determine"
    return None


def _render_user_prompt(*, question_text: str, gold_concepts: list[str],
                        agent_concepts: list[str]) -> str:
    return Template(_USER_PROMPT_PATH.read_text()).render(
        question_text=question_text,
        gold_concepts=gold_concepts,
        agent_concepts=agent_concepts,
    )


def _parse(text: str) -> Optional[list[_Verdict]]:
    parsed = extract_json(text)
    if not isinstance(parsed, dict):
        return None
    try:
        return _VerdictList.model_validate(parsed).verdicts
    except ValidationError as e:
        logger.warning("judge JSON failed schema: %s", e)
        return None


def _pair(verdicts: Optional[list[_Verdict]],
          gold_concepts: list[str]) -> list[Optional[_Verdict]]:
    """Line verdicts up with gold concepts: positional first, then by name."""
    if not verdicts:
        return [None] * len(gold_concepts)
    if len(verdicts) == len(gold_concepts):
        return list(verdicts)
    logger.warning("judge returned %d verdicts, expected %d; matching by name",
                   len(verdicts), len(gold_concepts))
    out: list[Optional[_Verdict]] = []
    for g in gold_concepts:
        target = g.strip().lower()
        hit = next((v for v in verdicts if v.gold_concept.strip().lower() == target), None)
        if hit is None:
            hit = next((v for v in verdicts
                        if v.gold_concept.strip().lower()[:40] == target[:40]), None)
        out.append(hit)
    return out


def _ask(client: Any, system_prompt: str, user_prompt: str) -> tuple[Optional[list[_Verdict]], str]:
    resp = client.generate(system_text=system_prompt, user_text=user_prompt,
                           max_tokens=16000)
    return _parse(resp.text), resp.text


def score_disambiguation(
    *,
    question_id: str,
    cohort: str,
    question_text: str,
    gold_concepts: list[str],
    agent_concepts: list[str],
    claude_client: Optional[VertexClient] = None,
    azure_client: Optional[AzureClient] = None,
    raw_log_dir: Optional[Path] = None,
) -> DisambiguationScoreResult:
    """Run both judges and sum their points per concept."""
    base = dict(question_id=question_id, cohort=cohort,
                question_text=question_text, scorer="llm_judge",
                points_per_concept=JUDGE_MAX_PER_CONCEPT)

    if not gold_concepts:
        return DisambiguationScoreResult(
            n_gold=0, points=0.0, decisions=[],
            agent_concepts=list(agent_concepts or []), **base)

    # No concepts from the agent: nothing addresses anything. Skip the calls.
    if not agent_concepts:
        decisions = [
            ConceptDecision(gold_concept=g, points=0.0,
                            labels={n: "no" for n in JUDGE_NAMES},
                            reasoning={n: "agent provided no concepts" for n in JUDGE_NAMES})
            for g in gold_concepts
        ]
        return DisambiguationScoreResult(
            n_gold=len(gold_concepts), points=0.0, decisions=decisions,
            agent_concepts=[], **base)

    system_prompt = _SYSTEM_PROMPT_PATH.read_text()
    user_prompt = _render_user_prompt(question_text=question_text,
                                      gold_concepts=gold_concepts,
                                      agent_concepts=agent_concepts)

    claude_client = claude_client or VertexClient.from_env()
    azure_client = azure_client or AzureClient.from_env()

    raw_by_judge: dict[str, str] = {}
    paired: dict[str, list[Optional[_Verdict]]] = {}
    for name, client in (("claude", claude_client), ("azure", azure_client)):
        try:
            verdicts, raw = _ask(client, system_prompt, user_prompt)
        except Exception as e:                                   # noqa: BLE001
            logger.warning("%s judge failed on %s: %s", name, question_id, e)
            verdicts, raw = None, f"<call failed: {e}>"
        raw_by_judge[name] = raw or ""
        paired[name] = _pair(verdicts, gold_concepts)

    if raw_log_dir is not None:
        try:
            raw_log_dir.mkdir(parents=True, exist_ok=True)
            for name, raw in raw_by_judge.items():
                (raw_log_dir / f"judge_{name}_raw.txt").write_text(raw)
        except Exception as e:                                   # noqa: BLE001
            logger.warning("Could not persist judge raw text: %s", e)

    decisions: list[ConceptDecision] = []
    total = 0.0
    n_missing = 0
    for i, g in enumerate(gold_concepts):
        labels: dict[str, str] = {}
        reasoning: dict[str, str] = {}
        pts = 0.0
        for name in JUDGE_NAMES:
            v = paired[name][i]
            answer = _normalise_answer(v.answer) if v else None
            if answer is None:
                n_missing += 1
                answer = "unable to determine"
                reasoning[name] = (v.reasoning if v and v.reasoning
                                   else "judge returned no usable verdict")
            else:
                reasoning[name] = (v.reasoning or "") if v else ""
            labels[name] = answer
            pts += label_points(answer)
        total += pts
        decisions.append(ConceptDecision(gold_concept=g, points=pts,
                                         labels=labels, reasoning=reasoning))

    return DisambiguationScoreResult(
        n_gold=len(gold_concepts), points=total, decisions=decisions,
        agent_concepts=list(agent_concepts), n_missing_verdicts=n_missing, **base)
