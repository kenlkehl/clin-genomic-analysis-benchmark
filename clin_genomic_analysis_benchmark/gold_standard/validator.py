"""Validate captured `result.json` files against the answer-type schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .answer_types import validate_answer


def load_and_validate(result_path: Path, answer_type: str) -> tuple[Optional[dict], list[str]]:
    """Read result.json and validate. Returns (result_dict, errors)."""
    if not result_path.exists():
        return None, [f"result file does not exist: {result_path}"]
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError as e:
        return None, [f"result file is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return None, [f"result must be a JSON object, got {type(data).__name__}"]
    errs = validate_answer(answer_type, data)
    return data, errs
