"""YAML load/save for the two per-cohort question banks.

There are two banks:
- **public** (`QUESTIONS_DIR`, in the repo): gold-free, agent-facing. Served to
  the agent under evaluation. Loaded by the eval path.
- **gold** (`gold_questions_dir()`, OUTSIDE the repo): the full bank with answers.
  Read only by the harness at scoring/compute time — never exposed to the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from ..config import QUESTIONS_DIR, gold_questions_dir
from ..utils.jsonio import atomic_write_text
from .schema import (
    CohortQuestionFile,
    PublicCohortQuestionFile,
    PublicQuestion,
)


# ---- paths -----------------------------------------------------------------

def public_yaml_path(cohort: str) -> Path:
    return QUESTIONS_DIR / f"{cohort}.yaml"


def gold_yaml_path(cohort: str) -> Path:
    return gold_questions_dir() / f"{cohort}.yaml"


# ---- gold bank (full, out-of-repo, harness-only) ---------------------------

def save_gold(cqf: CohortQuestionFile) -> Path:
    """Save the full gold question file atomically (out-of-repo gold root)."""
    path = gold_yaml_path(cqf.cohort)
    payload = cqf.model_dump(mode="json", exclude_none=True)
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return path


def load_gold(cohort: str) -> Optional[CohortQuestionFile]:
    """Load and validate the full gold bank, or None if missing."""
    path = gold_yaml_path(cohort)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text())
    return CohortQuestionFile.model_validate(raw)


# ---- public bank (gold-free, in-repo, agent-facing) ------------------------

def to_public(cqf: CohortQuestionFile) -> PublicCohortQuestionFile:
    """Project a full gold file down to the gold-free public file."""
    return PublicCohortQuestionFile(
        cohort=cqf.cohort,
        generated_at=cqf.generated_at,
        model=cqf.model,
        schema_version=cqf.schema_version,
        questions=[PublicQuestion(id=q.id, category=q.category, text=q.text)
                   for q in cqf.questions],
    )


def save_public(pcqf: PublicCohortQuestionFile) -> Path:
    """Save the gold-free public question file atomically (in the repo)."""
    path = public_yaml_path(pcqf.cohort)
    payload = pcqf.model_dump(mode="json", exclude_none=True)
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return path


def load_public(cohort: str) -> Optional[PublicCohortQuestionFile]:
    """Load and validate the gold-free public bank, or None if missing."""
    path = public_yaml_path(cohort)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text())
    return PublicCohortQuestionFile.model_validate(raw)


# ---- merge (gold bank; used by question generation) ------------------------

def merge_questions(existing: CohortQuestionFile, new: CohortQuestionFile) -> CohortQuestionFile:
    """Merge new questions into an existing file, preserving review_status and notes for matching IDs."""
    by_id = {q.id: q for q in existing.questions}
    merged: list = []
    for q in new.questions:
        prev = by_id.get(q.id)
        if prev is not None and prev.review_status != "draft":
            # Keep the curated version
            merged.append(prev)
        else:
            merged.append(q)
        by_id.pop(q.id, None)
    # Preserve any reviewed questions not present in the new batch
    for prev in by_id.values():
        if prev.review_status == "reviewed":
            merged.append(prev)
    return CohortQuestionFile(
        cohort=existing.cohort,
        generated_at=new.generated_at,
        model=new.model,
        schema_version=new.schema_version,
        questions=merged,
    )
