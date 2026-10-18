"""Tests for the Claude Code/local-vLLM Gemma 4 adapter."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.claude_code_vllm_gemma4 import adapter
from clin_genomic_analysis_benchmark.agent import isolation
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


def _call_adapter() -> str:
    return adapter._claude_call(
        system_prompt="system",
        user_prompt="user",
        cohort_dir="/cohort",
        data_dictionary_path="/dictionary.xlsx",
        scratch_dir="/scratch",
        allowed_tools="Read,Glob,Grep,Bash",
    )


@pytest.fixture
def captured(monkeypatch):
    captured: dict = {}

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        captured["home_kind"] = kwargs["home_kind"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *args, **kwargs: [])
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000/v1")
    monkeypatch.setenv("VLLM_MODEL", "test-gemma")
    monkeypatch.delenv("VLLM_TOKEN", raising=False)
    return captured


def _complete_with(monkeypatch, stdout: str, returncode: int = 0) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode, stdout, ""),
    )


def test_base_url_drops_v1_and_rejects_credentials(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000/v1/")
    assert adapter._anthropic_base_url() == "http://vllm.internal:8000"

    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000")
    assert adapter._anthropic_base_url() == "http://vllm.internal:8000"

    monkeypatch.setenv("VLLM_BASE_URL", "http://user:secret@vllm.internal:8000")
    with pytest.raises(adapter.ClaudeInvocationError, match="credentials"):
        adapter._anthropic_base_url()

    monkeypatch.delenv("VLLM_BASE_URL")
    with pytest.raises(adapter.ClaudeInvocationError, match="VLLM_BASE_URL"):
        adapter._anthropic_base_url()


def test_claude_call_routes_every_model_alias_to_vllm(monkeypatch, captured):
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID",
                 "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setenv(name, "host-value")
    _complete_with(monkeypatch, json.dumps({"type": "result", "result": '{"a": 1}'}))

    assert _call_adapter() == '{"a": 1}'

    command, env = captured["cmd"], captured["env"]
    assert command[command.index("--model") + 1] == "test-gemma"
    assert "--effort" not in command
    assert captured["home_kind"] == "claude_vllm"
    assert env["ANTHROPIC_BASE_URL"] == "http://vllm.internal:8000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == adapter.PLACEHOLDER_AUTH_TOKEN
    for name in adapter.MODEL_ALIAS_ENV:
        assert env[name] == "test-gemma"
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID",
                 "ANTHROPIC_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        assert name not in env


def test_model_aliases_survive_sandbox_environment_filter():
    source = {name: "test-gemma" for name in adapter.MODEL_ALIAS_ENV}
    filtered = isolation._sandbox_environment(source)
    for name in adapter.MODEL_ALIAS_ENV:
        assert filtered[name] == "test-gemma"


def test_claude_vllm_home_has_no_cloud_credentials(tmp_path, monkeypatch):
    credential = tmp_path / "adc.json"
    credential.write_text("{}")
    home = tmp_path / "home"
    home.mkdir()

    isolation._seed_home(
        home, "claude_vllm", {"GOOGLE_APPLICATION_CREDENTIALS": str(credential)}
    )

    assert (home / ".claude/settings.json").read_text() == "{}"
    assert not (home / ".config/gcloud").exists()


def test_upstream_token_is_refused(monkeypatch, captured):
    monkeypatch.setenv("VLLM_TOKEN", "real-secret")
    with pytest.raises(adapter.ClaudeInvocationError, match="unauthenticated"):
        _call_adapter()
    assert "cmd" not in captured


def test_error_result_is_a_failure(monkeypatch, captured):
    _complete_with(
        monkeypatch,
        json.dumps({"type": "result", "is_error": True, "result": "model not found"}),
    )
    with pytest.raises(adapter.ClaudeInvocationError, match="model not found"):
        _call_adapter()


def test_adapter_is_registered_and_provenance_records_endpoint():
    command = "bash adapters/claude_code_vllm_gemma4/run.sh"
    assert require_supported_adapter(command) == "claude_code_vllm_gemma4"

    provenance = _agent_provenance(
        command,
        environ={
            "VLLM_BASE_URL": "http://vllm.internal:8000",
            "VLLM_MODEL": "test-gemma",
            "CLINGEN_CLAUDE_MODEL": "claude-opus-4-8",
        },
    )

    assert provenance["adapter"] == "claude_code_vllm_gemma4"
    assert provenance["client"] == "claude_code"
    assert provenance["model"] == "test-gemma"
    assert provenance["base_url"] == "http://vllm.internal:8000/v1"
    assert provenance["wire_api"] == "anthropic_messages"
    assert provenance["effort_supported"] is False
    assert provenance["environment"] == {
        "VLLM_BASE_URL": "http://vllm.internal:8000",
        "VLLM_MODEL": "test-gemma",
    }
