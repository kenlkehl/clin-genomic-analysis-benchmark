"""Dual-LLM judge for the disambiguation subtask.

For each ambiguous question:
  - Both judges (Claude/Vertex + Azure OpenAI) receive the question + gold concepts +
    agent's concepts and return per-gold-concept covered:bool.
  - If they agree on a concept, record it (1 pt covered, 0 pt not).
  - If they disagree, write to review_queue.yaml with provisional 0.5 pt and let the
    human resolve via `clingen-bench score --resolve-reviews`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from jinja2 import Template
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import PROMPTS_DIR
from ..llm.azure_openai_client import AzureClient
from ..llm.vertex_client import VertexClient
from ..utils.jsonio import atomic_write_text, extract_json

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "judge_disambiguation_system.md"
_USER_PROMPT_PATH = PROMPTS_DIR / "judge_disambiguation_user.md.j2"


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gold_concept: str
    covered: bool
    justification: str = ""


class _VerdictList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdicts: list[_Verdict]


@dataclass
class ConceptDecision:
    gold_concept: str
    decision: str            # "covered" | "not_covered" | "needs_review"
    points: float            # 1.0 covered, 0.0 not, 0.5 provisional
    claude_covered: Optional[bool]
    azure_covered: Optional[bool]
    claude_justification: str = ""
    azure_justification: str = ""


@dataclass
class DisambiguationScoreResult:
    question_id: str
    cohort: str
    n_gold: int
    n_covered: int
    n_disagreed: int
    points: float
    decisions: list[ConceptDecision]
    # Context carried through so write_review_queue can emit a self-contained
    # entry for human review without forcing the reviewer to open separate files.
    question_text: str = ""
    agent_concepts: list[str] = field(default_factory=list)


def _render_user_prompt(*, question_text: str, gold_concepts: list[str],
                        agent_concepts: list[str]) -> str:
    tmpl = Template(_USER_PROMPT_PATH.read_text())
    return tmpl.render(
        question_text=question_text,
        gold_concepts=gold_concepts,
        agent_concepts=agent_concepts,
    )


def _parse_verdicts(text: str, expected_n: int) -> Optional[list[_Verdict]]:
    parsed = extract_json(text)
    if not isinstance(parsed, dict):
        return None
    try:
        wrapped = _VerdictList.model_validate(parsed)
    except ValidationError as e:
        logger.warning("judge JSON failed schema: %s", e)
        return None
    if len(wrapped.verdicts) != expected_n:
        logger.warning("judge returned %d verdicts but expected %d",
                       len(wrapped.verdicts), expected_n)
        # Return what we got; mismatched concepts → safer to mark as disagreement
    return wrapped.verdicts


def _judge_with_claude(client: VertexClient, system_prompt: str, user_prompt: str,
                       expected_n: int) -> tuple[Optional[list[_Verdict]], str]:
    resp = client.generate(
        system_text=system_prompt, user_text=user_prompt, max_tokens=16000,
    )
    return _parse_verdicts(resp.text, expected_n), resp.text


def _judge_with_azure(client: AzureClient, system_prompt: str, user_prompt: str,
                      expected_n: int) -> tuple[Optional[list[_Verdict]], str]:
    resp = client.generate(
        system_text=system_prompt, user_text=user_prompt, max_tokens=16000,
    )
    return _parse_verdicts(resp.text, expected_n), resp.text


def _verdict_for(verdicts: Optional[list[_Verdict]], gold_concept: str) -> Optional[_Verdict]:
    if not verdicts:
        return None
    for v in verdicts:
        if v.gold_concept.strip().lower() == gold_concept.strip().lower():
            return v
    # Relaxed fallback: prefix match on first 40 chars (judges occasionally
    # truncate the parenthetical examples in the verbatim copy).
    target = gold_concept.strip().lower()[:40]
    for v in verdicts:
        if v.gold_concept.strip().lower()[:40] == target:
            return v
    return None


def _verdicts_by_index(verdicts: Optional[list[_Verdict]],
                       gold_concepts: list[str]) -> list[Optional[_Verdict]]:
    """Pair verdicts with gold concepts.

    Preferred: positional pairing when the judge returned exactly len(gold)
    verdicts (the prompt asks for "one entry per gold concept (in order)").
    Fallback: name-based matching via `_verdict_for`. Empty/None judge response
    returns all-None.
    """
    if not verdicts:
        return [None] * len(gold_concepts)
    if len(verdicts) == len(gold_concepts):
        return list(verdicts)
    return [_verdict_for(verdicts, g) for g in gold_concepts]


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
    """Run both judges and synthesise per-concept decisions."""
    if not gold_concepts:
        return DisambiguationScoreResult(
            question_id=question_id, cohort=cohort, n_gold=0,
            n_covered=0, n_disagreed=0, points=0.0, decisions=[],
            question_text=question_text, agent_concepts=list(agent_concepts or []),
        )
    # If the agent provided no concepts, mark every gold as not_covered (no judge calls).
    if not agent_concepts:
        decisions = [
            ConceptDecision(g, "not_covered", 0.0, claude_covered=False,
                            azure_covered=False, claude_justification="agent provided no concepts",
                            azure_justification="agent provided no concepts")
            for g in gold_concepts
        ]
        return DisambiguationScoreResult(
            question_id=question_id, cohort=cohort, n_gold=len(gold_concepts),
            n_covered=0, n_disagreed=0, points=0.0, decisions=decisions,
            question_text=question_text, agent_concepts=[],
        )

    system_prompt = _SYSTEM_PROMPT_PATH.read_text()
    user_prompt = _render_user_prompt(
        question_text=question_text,
        gold_concepts=gold_concepts,
        agent_concepts=agent_concepts,
    )

    claude_client = claude_client or VertexClient.from_env()
    azure_client = azure_client or AzureClient.from_env()

    claude_verdicts, claude_raw = _judge_with_claude(claude_client, system_prompt, user_prompt, len(gold_concepts))
    azure_verdicts, azure_raw = _judge_with_azure(azure_client, system_prompt, user_prompt, len(gold_concepts))

    if raw_log_dir is not None:
        try:
            raw_log_dir.mkdir(parents=True, exist_ok=True)
            (raw_log_dir / "judge_claude_raw.txt").write_text(claude_raw or "")
            (raw_log_dir / "judge_azure_raw.txt").write_text(azure_raw or "")
        except Exception as e:
            logger.warning("Could not persist judge raw text: %s", e)

    decisions: list[ConceptDecision] = []
    n_covered = 0
    n_disagreed = 0
    points = 0.0
    claude_paired = _verdicts_by_index(claude_verdicts, gold_concepts)
    azure_paired = _verdicts_by_index(azure_verdicts, gold_concepts)
    for i, g in enumerate(gold_concepts):
        cv = claude_paired[i]
        av = azure_paired[i]
        c_cov = cv.covered if cv else None
        a_cov = av.covered if av else None
        if c_cov is None and a_cov is None:
            d = ConceptDecision(g, "needs_review", 0.5, c_cov, a_cov,
                                "judge returned no verdict for this concept",
                                "judge returned no verdict for this concept")
            n_disagreed += 1
            points += 0.5
        elif c_cov is None or a_cov is None:
            # One judge missed; treat as disagreement
            d = ConceptDecision(g, "needs_review", 0.5, c_cov, a_cov,
                                cv.justification if cv else "missing",
                                av.justification if av else "missing")
            n_disagreed += 1
            points += 0.5
        elif c_cov == a_cov:
            decision = "covered" if c_cov else "not_covered"
            pts = 1.0 if c_cov else 0.0
            d = ConceptDecision(g, decision, pts, c_cov, a_cov,
                                cv.justification, av.justification)
            if c_cov:
                n_covered += 1
            points += pts
        else:
            d = ConceptDecision(g, "needs_review", 0.5, c_cov, a_cov,
                                cv.justification, av.justification)
            n_disagreed += 1
            points += 0.5
        decisions.append(d)

    return DisambiguationScoreResult(
        question_id=question_id, cohort=cohort,
        n_gold=len(gold_concepts), n_covered=n_covered,
        n_disagreed=n_disagreed, points=points, decisions=decisions,
        question_text=question_text, agent_concepts=list(agent_concepts),
    )


# ---- review queue persistence ----

def write_review_queue(decisions: list[DisambiguationScoreResult], path: Path) -> Path:
    """Write all `needs_review` decisions to a YAML file for manual adjudication.

    Each entry includes the question text and the agent's full disambiguation
    output so the human reviewer can decide without opening separate files.
    """
    entries: list[dict] = []
    for r in decisions:
        for d in r.decisions:
            if d.decision == "needs_review":
                entries.append({
                    "question_id": r.question_id,
                    "cohort": r.cohort,
                    "question_text": r.question_text,
                    "agent_concepts": list(r.agent_concepts),
                    "gold_concept": d.gold_concept,
                    "claude_covered": d.claude_covered,
                    "azure_covered": d.azure_covered,
                    "claude_justification": d.claude_justification,
                    "azure_justification": d.azure_justification,
                    "human_decision": None,    # fill in: true|false
                })
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))
    return path


def apply_human_resolutions(scored_results: list[DisambiguationScoreResult], queue_path: Path) -> int:
    """Apply human decisions from the queue file (idempotent). Returns # resolved."""
    if not queue_path.exists():
        return 0
    raw = yaml.safe_load(queue_path.read_text()) or []
    resolved = 0
    by_key: dict[tuple[str, str], dict] = {}
    for entry in raw:
        if entry.get("human_decision") is None:
            continue
        by_key[(entry["question_id"], entry["gold_concept"].strip().lower())] = entry
    for r in scored_results:
        for d in r.decisions:
            if d.decision != "needs_review":
                continue
            key = (r.question_id, d.gold_concept.strip().lower())
            entry = by_key.get(key)
            if entry is None:
                continue
            human_dec = bool(entry["human_decision"])
            old_pts = d.points
            d.decision = "covered" if human_dec else "not_covered"
            d.points = 1.0 if human_dec else 0.0
            r.points += d.points - old_pts
            if human_dec:
                r.n_covered += 1
            r.n_disagreed -= 1
            resolved += 1
    return resolved
