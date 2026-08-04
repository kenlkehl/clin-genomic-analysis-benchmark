"""Focused tests for Claude Code adapter process diagnostics."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1] / "adapters" / "claude_code" / "adapter.py"
)
_SPEC = importlib.util.spec_from_file_location("claude_code_adapter", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)


def _call_adapter() -> str:
    return adapter._claude_call(
        system_prompt="system",
        user_prompt="user",
        cohort_dir="/cohort",
        scratch_dir="/scratch",
        allowed_tools="Read",
    )


def test_claude_failure_preserves_structured_stdout(monkeypatch):
    provider_error = (
        '{"type":"result","is_error":true,'
        '"result":"Permission denied on resource project test-project"}'
    )
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout=provider_error, stderr=""
    )
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "real-project")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(adapter.ClaudeInvocationError) as exc_info:
        _call_adapter()

    message = str(exc_info.value)
    assert "claude exited 1" in message
    assert "--- stdout ---" in message
    assert "Permission denied on resource project test-project" in message


def test_claude_failure_preserves_stderr(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=2, stdout="", stderr="model not found"
    )
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "real-project")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(adapter.ClaudeInvocationError, match="model not found"):
        _call_adapter()


def test_vertex_project_placeholder_fails_before_subprocess(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "YOUR_GCP_PROJECT_ID")

    def unexpected_run(*args, **kwargs):
        pytest.fail("Claude subprocess should not run with a placeholder project ID")

    monkeypatch.setattr(adapter.subprocess, "run", unexpected_run)

    with pytest.raises(adapter.ClaudeInvocationError, match="still a placeholder"):
        _call_adapter()


def test_explicit_effort_is_passed_to_claude(monkeypatch):
    captured = {}
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout='{"result":"done"}', stderr=""
    )
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "real-project")
    monkeypatch.setenv("CLINGEN_CLAUDE_MODEL", "claude-sonnet-5@20260203")
    monkeypatch.setenv("CLINGEN_CLAUDE_EFFORT", "xhigh")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    assert _call_adapter() == "done"
    effort_index = captured["cmd"].index("--effort")
    assert captured["cmd"][effort_index + 1] == "xhigh"


def test_explicit_effort_rejected_for_haiku(monkeypatch):
    monkeypatch.setenv("CLINGEN_CLAUDE_MODEL", "claude-haiku-4-5@20251001")
    monkeypatch.setenv("CLINGEN_CLAUDE_EFFORT", "xhigh")

    with pytest.raises(adapter.ClaudeInvocationError, match="does not support"):
        _call_adapter()
