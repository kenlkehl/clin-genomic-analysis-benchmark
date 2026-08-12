"""Focused tests for the Codex/Qwen 3.6 27B local-Unsloth adapter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import (
    _agent_provenance,
    _timeout_config_for_adapter,
)
from clin_genomic_analysis_benchmark.config import TimeoutConfig


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_NAME = "codex_qwen_3.6_27B_GGUF_Unsloth_q4bitxl"
ADAPTER_PATH = REPO_ROOT / "adapters" / ADAPTER_NAME / "adapter.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_qwen_27b_unsloth_adapter", ADAPTER_PATH
)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


def test_27b_defaults_match_loaded_unsloth_model(monkeypatch):
    monkeypatch.delenv("UNSLOTH_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("UNSLOTH_STUDIO_BASE_URL", raising=False)

    assert adapter._model() == "unsloth/Qwen3.6-27B-MTP-GGUF"
    assert adapter._base_url() == "http://127.0.0.1:8888/v1"
    assert adapter.PROVIDER_NAME == "local_unsloth_qwen3_6_27b"
    assert adapter.MODEL_CONTEXT_WINDOW == 262_144


def test_27b_adapter_is_registered_with_timeouts_and_provenance():
    command = f"bash adapters/{ADAPTER_NAME}/run.sh"
    assert require_supported_adapter(command) == ADAPTER_NAME
    assert _timeout_config_for_adapter(ADAPTER_NAME) == TimeoutConfig(
        classify=3_600,
        disambiguate=3_600,
        analyze=7_200,
    )

    provenance = _agent_provenance(command, environ={})
    assert provenance["adapter"] == ADAPTER_NAME
    assert provenance["provider"] == adapter.PROVIDER_NAME
    assert provenance["model"] == adapter.DEFAULT_MODEL
    assert provenance["model_source"] == "adapter_default"
    assert provenance["base_url"] == adapter.DEFAULT_BASE_URL
    assert provenance["context_window_tokens"] == 262_144
    assert provenance["authentication_boundary"] == "trusted_local_bridge"
    assert provenance["environment"] == {}
