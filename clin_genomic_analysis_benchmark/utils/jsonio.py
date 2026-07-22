"""Robust JSON extraction and atomic writes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional


def extract_json(text: str) -> Optional[dict | list]:
    """Pull the first valid JSON object/array out of a possibly-noisy LLM response."""
    text = text.strip()
    if not text:
        return None
    # Whole-text fast path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ```json ... ``` fenced block
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # First {...} or [...] balanced span (greedy, then shrinking)
    for start_char, end_char in (("{", "}"), ("[", "]")):
        if start_char not in text:
            continue
        start = text.index(start_char)
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    return None


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (write to tmp, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, default=str))
