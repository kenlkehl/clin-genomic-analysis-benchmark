"""The rulebooks served to models under evaluation.

Each task has its own rulebook, and both share the conventions:

- assess:  ``ASSESS_INSTRUCTIONS.md`` + ``CONVENTIONS.md``
- analyze: ``ANALYZE_INSTRUCTIONS.md`` + ``CONVENTIONS.md``

The files live at the repo root so reviewers edit them as plain Markdown.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import REPO_ROOT

ASSESS_INSTRUCTIONS_PATH = REPO_ROOT / "ASSESS_INSTRUCTIONS.md"
ANALYZE_INSTRUCTIONS_PATH = REPO_ROOT / "ANALYZE_INSTRUCTIONS.md"
CONVENTIONS_PATH = REPO_ROOT / "CONVENTIONS.md"

_SEPARATOR = "\n\n---\n\n"


def _join(*paths: Path) -> str:
    return _SEPARATOR.join(path.read_text().strip("\n") for path in paths) + "\n"


def assess_rulebook() -> str:
    """The assess rulebook followed by the shared conventions."""
    return _join(ASSESS_INSTRUCTIONS_PATH, CONVENTIONS_PATH)


def analyze_rulebook() -> str:
    """The analyze rulebook followed by the shared conventions."""
    return _join(ANALYZE_INSTRUCTIONS_PATH, CONVENTIONS_PATH)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rulebook_provenance(task: str) -> dict[str, str]:
    """SHA-256 of each rulebook file a task's prompt is built from."""
    if task == "assess":
        own = ASSESS_INSTRUCTIONS_PATH
    elif task == "analyze":
        own = ANALYZE_INSTRUCTIONS_PATH
    else:
        raise ValueError(f"unknown task: {task!r}")
    return {own.name: _sha256(own), CONVENTIONS_PATH.name: _sha256(CONVENTIONS_PATH)}
