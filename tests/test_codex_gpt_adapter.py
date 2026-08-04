"""Focused tests for explicit Codex CLI runtime selection."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1] / "adapters" / "codex_gpt" / "adapter.py"
)
_SPEC = importlib.util.spec_from_file_location("codex_gpt_adapter", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)


def test_codex_call_passes_explicit_provider_model_and_effort(tmp_path, monkeypatch):
    captured = {}
    scratch_dir = tmp_path / "scratch"
    cohort_dir = tmp_path / "cohort"
    cohort_dir.mkdir()
    completed = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout='{"classification":"ambiguous"}', stderr=""
    )
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CODEX_MODEL_PROVIDER", "azure")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    monkeypatch.delenv("CODEX_PROFILE", raising=False)
    monkeypatch.setattr(adapter, "_refresh_azure_token", lambda env: None)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result = adapter._codex_call(
        prompt="prompt",
        question={
            "stage": "classify",
            "scratch_dir": str(scratch_dir),
            "cohort_dir": str(cohort_dir),
        },
        last_message_file=scratch_dir / "last-message.txt",
    )

    assert result == completed.stdout
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_provider="azure"' in captured["cmd"]
    assert 'model_reasoning_effort="xhigh"' in captured["cmd"]
