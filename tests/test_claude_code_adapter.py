"""Focused tests for Claude Code adapter process diagnostics."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
        data_dictionary_path="/dictionary.xlsx",
        scratch_dir="/scratch",
        allowed_tools="Read",
    )


@pytest.fixture
def _stub_outer_sandbox(monkeypatch):
    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(
        adapter, "export_agent_session_audit", lambda *args, **kwargs: []
    )


def test_claude_failure_preserves_structured_stdout(monkeypatch, _stub_outer_sandbox):
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


def test_claude_failure_preserves_stderr(monkeypatch, _stub_outer_sandbox):
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=2, stdout="", stderr="model not found"
    )
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "real-project")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(adapter.ClaudeInvocationError, match="model not found"):
        _call_adapter()


def test_vertex_project_placeholder_fails_before_subprocess(
    monkeypatch, _stub_outer_sandbox
):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "YOUR_GCP_PROJECT_ID")

    def unexpected_run(*args, **kwargs):
        pytest.fail("Claude subprocess should not run with a placeholder project ID")

    monkeypatch.setattr(adapter.subprocess, "run", unexpected_run)

    with pytest.raises(adapter.ClaudeInvocationError, match="still a placeholder"):
        _call_adapter()


def test_explicit_effort_is_passed_to_claude(monkeypatch, _stub_outer_sandbox):
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


def test_explicit_effort_rejected_for_haiku(monkeypatch, _stub_outer_sandbox):
    monkeypatch.setenv("CLINGEN_CLAUDE_MODEL", "claude-haiku-4-5@20251001")
    monkeypatch.setenv("CLINGEN_CLAUDE_EFFORT", "xhigh")

    with pytest.raises(adapter.ClaudeInvocationError, match="does not support"):
        _call_adapter()


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="bwrap unavailable")
def test_claude_adapter_cannot_read_sibling_gold_file(tmp_path, monkeypatch):
    cohort = tmp_path / "cohort"
    scratch = tmp_path / "question" / "scratch"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    forbidden = tmp_path / "gold-answer.json"
    forbidden.write_text('{"answer": 42}')

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"forbidden = {str(forbidden.resolve())!r}\n"
        "answer = {'classification': 'ambiguous', "
        "'rationale': 'visible' if os.path.exists(forbidden) else 'hidden'}\n"
        "print(json.dumps({'result': json.dumps(answer)}))\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake_claude))
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "real-project")
    monkeypatch.delenv("CLINGEN_CLAUDE_EFFORT", raising=False)

    text = adapter._claude_call(
        system_prompt="system",
        user_prompt="user",
        cohort_dir=str(cohort),
        data_dictionary_path=str(dictionary),
        scratch_dir=str(scratch),
        allowed_tools="Read",
    )

    assert json.loads(text)["rationale"] == "hidden"
