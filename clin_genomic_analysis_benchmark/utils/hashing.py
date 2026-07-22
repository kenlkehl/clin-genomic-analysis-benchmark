"""Stable hashing for question / file IDs."""

from __future__ import annotations

import hashlib


def question_id(cohort: str, category: int, text: str, *, prefix: str = "Q") -> str:
    """Deterministic short id: <cohort>-Q<8hex>.  Stable across runs."""
    h = hashlib.sha1(f"{cohort}|{category}|{text.strip()}".encode("utf-8")).hexdigest()[:8]
    return f"{cohort}-{prefix}{h}"
