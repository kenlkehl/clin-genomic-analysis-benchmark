"""Question generation driver (Piece 1)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Template
from pydantic import ValidationError

from .. import data_dictionary, sampling
from ..cohorts import Cohort
from ..config import PROMPTS_DIR, settings
from ..llm.vertex_client import CachedBlock, VertexClient
from ..utils.hashing import question_id
from ..utils.jsonio import extract_json
from . import categories as cat_mod
from . import io as q_io
from .balancer import balance
from .schema import (
    AnalysisSpec,
    CohortQuestionFile,
    Question,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "question_gen_system.md"
_USER_PROMPT_PATH = PROMPTS_DIR / "question_gen_user.md.j2"


def _load_prompts() -> tuple[str, Template]:
    system = _SYSTEM_PROMPT_PATH.read_text()
    user_tmpl = Template(_USER_PROMPT_PATH.read_text())
    return system, user_tmpl


def _build_cached_blocks(cohort: Cohort) -> list[CachedBlock]:
    """Build the cached prompt blocks for a cohort: dictionary + cohort context."""
    variables = data_dictionary.load(cohort)
    ctx = sampling.build(cohort)
    dict_md = data_dictionary.to_compact_markdown(variables)
    ctx_md = sampling.to_compact_markdown(ctx)
    return [
        CachedBlock(
            text=f"# COHORT DATA DICTIONARY ({cohort.name})\n\n" + dict_md,
            label="dictionary",
        ),
        CachedBlock(
            text=f"# COHORT FILE INVENTORY & SAMPLES ({cohort.name})\n\n" + ctx_md,
            label="cohort_context",
        ),
    ]


def _candidates_to_questions(cohort: Cohort, category_id: int, items: list[dict]) -> list[Question]:
    """Convert raw LLM output items into validated Question objects."""
    out: list[Question] = []
    seen_ids: set[str] = set()
    for raw in items:
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        qid = question_id(cohort.name, category_id, text)
        if qid in seen_ids:
            continue
        seen_ids.add(qid)

        classification = raw.get("classification")
        if classification not in ("ambiguous", "unambiguous"):
            logger.warning("Skipping question with bad classification: %r", raw.get("classification"))
            continue

        spec_obj = None
        if classification == "unambiguous":
            spec_raw = raw.get("analysis_spec") or {}
            try:
                spec_obj = AnalysisSpec.model_validate(spec_raw)
            except ValidationError as e:
                logger.warning("Skipping unambiguous question with bad analysis_spec for %s: %s", qid, e)
                continue

        concepts = None
        if classification == "ambiguous":
            concepts = [c.strip() for c in (raw.get("disambiguation_concepts") or []) if c and c.strip()]
            if not concepts:
                logger.warning("Skipping ambiguous question with no disambiguation_concepts: %s", qid)
                continue

        try:
            q = Question(
                id=qid,
                category=category_id,
                text=text,
                classification=classification,
                rationale=(raw.get("rationale") or "").strip() or None,
                analysis_spec=spec_obj,
                disambiguation_concepts=concepts,
                source="llm",
                review_status="draft",
            )
        except ValidationError as e:
            logger.warning("Skipping invalid question payload %s: %s", qid, e)
            continue
        out.append(q)
    return out


def generate_for_category(
    *,
    client: VertexClient,
    cohort: Cohort,
    category_id: int,
    n_per_category: int = 5,
    target_ambiguous_frac: float = 0.5,
    overshoot: float = 1.6,
    max_top_up_iters: int = 1,
    existing_texts: Optional[list[str]] = None,
) -> list[Question]:
    """Generate (and balance) `n_per_category` questions for one (cohort, category)."""
    system_prompt, user_tmpl = _load_prompts()
    cached_blocks = _build_cached_blocks(cohort)
    category = cat_mod.get(category_id)

    target_amb = round(n_per_category * target_ambiguous_frac)
    target_unamb = n_per_category - target_amb
    n_to_request = max(n_per_category, int(round(n_per_category * overshoot)))

    user_prompt = user_tmpl.render(
        cohort_name=cohort.name,
        cohort_label=cohort.label,
        category=category,
        n_to_request=n_to_request,
        ambiguous_target=target_amb,
        unambiguous_target=target_unamb,
        existing_question_texts=existing_texts or [],
    )

    logger.info(
        "Generating questions: cohort=%s category=%d (%s) requesting=%d",
        cohort.name, category_id, category.name, n_to_request,
    )
    resp = client.generate(
        system_text=system_prompt,
        cached_blocks=cached_blocks,
        user_text=user_prompt,
        max_tokens=8000,
    )
    logger.info(
        "  tokens: in=%d out=%d cache_read=%d cache_create=%d",
        resp.input_tokens, resp.output_tokens, resp.cache_read_tokens, resp.cache_creation_tokens,
    )

    parsed = extract_json(resp.text)
    if not isinstance(parsed, dict) or "questions" not in parsed:
        logger.error("LLM returned invalid JSON for %s/cat%d. Raw: %s",
                     cohort.name, category_id, resp.text[:1000])
        return []

    candidates = _candidates_to_questions(cohort, category_id, parsed["questions"])

    selected, stats = balance(candidates, n_per_category, target_ambiguous_frac)
    logger.info("  balanced: %s", stats)

    # Top-up loop if shortfall
    iters = 0
    cur_texts = (existing_texts or []) + [q.text for q in selected]
    while stats["shortfall"] > 0 and iters < max_top_up_iters:
        iters += 1
        deficit = stats["shortfall"]
        top_user = user_tmpl.render(
            cohort_name=cohort.name,
            cohort_label=cohort.label,
            category=category,
            n_to_request=max(deficit * 2, 4),
            ambiguous_target=max(stats["target_ambiguous"] - stats["selected_ambiguous"], 0),
            unambiguous_target=max(stats["target_unambiguous"] - stats["selected_unambiguous"], 0),
            existing_question_texts=cur_texts,
        )
        logger.info("  top-up iter %d: requesting %d more", iters, deficit)
        top_resp = client.generate(
            system_text=system_prompt,
            cached_blocks=cached_blocks,
            user_text=top_user,
            max_tokens=8000,
        )
        top_parsed = extract_json(top_resp.text)
        if isinstance(top_parsed, dict) and "questions" in top_parsed:
            extras = _candidates_to_questions(cohort, category_id, top_parsed["questions"])
            combined, stats = balance(selected + extras, n_per_category, target_ambiguous_frac)
            selected = combined
            cur_texts = (existing_texts or []) + [q.text for q in selected]

    return selected


def generate_for_cohort(
    *,
    cohort: Cohort,
    n_per_category: int = 5,
    target_ambiguous_frac: float = 0.5,
    only_categories: Optional[list[int]] = None,
    force: bool = False,
) -> Path:
    """Generate questions for all 8 categories (or a subset) for one cohort.

    If a YAML already exists, preserves any human-`reviewed` questions and only
    overwrites `draft` questions for the requested categories — unless `force` is set.
    """
    client = VertexClient.from_env()
    cats = only_categories or [c.id for c in cat_mod.CATEGORIES]
    existing = q_io.load_gold(cohort.name)

    new_questions: list[Question] = []
    # Carry over reviewed questions verbatim, and any draft questions in
    # categories we are not regenerating.
    if existing is not None and not force:
        for q in existing.questions:
            if q.review_status == "reviewed" or q.category not in cats:
                new_questions.append(q)

    for cid in cats:
        existing_texts = [q.text for q in new_questions if q.category == cid]
        cat_qs = generate_for_category(
            client=client,
            cohort=cohort,
            category_id=cid,
            n_per_category=n_per_category,
            target_ambiguous_frac=target_ambiguous_frac,
            existing_texts=existing_texts,
        )
        new_questions.extend(cat_qs)

    cqf = CohortQuestionFile(
        cohort=cohort.name,
        generated_at=datetime.now(timezone.utc),
        model=f"{settings().claude.model}@vertex",
        questions=new_questions,
    )
    return q_io.save_gold(cqf)
