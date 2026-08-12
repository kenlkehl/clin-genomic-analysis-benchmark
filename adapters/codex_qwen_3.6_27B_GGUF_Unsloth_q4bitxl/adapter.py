"""Codex CLI adapter for Qwen 3.6 27B on local Unsloth Studio.

The request bridge, contract recovery, retries, and isolation behavior are kept
in the 35B-A3B adapter implementation so both local Qwen variants remain
behaviorally identical. Only the provider identity and served model differ.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ADAPTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = ADAPTER_DIR.parent.parent
IMPLEMENTATION_PATH = (
    REPO_ROOT
    / "adapters"
    / "codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl"
    / "adapter.py"
)

PROVIDER_NAME = "local_unsloth_qwen3_6_27b"
PROVIDER_DISPLAY_NAME = "Local Unsloth Studio (Qwen 3.6 27B)"
DEFAULT_MODEL = "unsloth/Qwen3.6-27B-MTP-GGUF"
DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"
MODEL_CONTEXT_WINDOW = 262_144


def _load_implementation() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_codex_qwen_unsloth_27b_implementation", IMPLEMENTATION_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load shared Qwen adapter: {IMPLEMENTATION_PATH}")
    implementation = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = implementation
    spec.loader.exec_module(implementation)
    implementation.PROVIDER_NAME = PROVIDER_NAME
    implementation.PROVIDER_DISPLAY_NAME = PROVIDER_DISPLAY_NAME
    implementation.DEFAULT_MODEL = DEFAULT_MODEL
    implementation.DEFAULT_BASE_URL = DEFAULT_BASE_URL
    implementation.MODEL_CONTEXT_WINDOW = MODEL_CONTEXT_WINDOW
    return implementation


_implementation = _load_implementation()
main = _implementation.main


def __getattr__(name: str):
    """Expose implementation helpers for diagnostics and focused tests."""
    return getattr(_implementation, name)


if __name__ == "__main__":
    raise SystemExit(main())
