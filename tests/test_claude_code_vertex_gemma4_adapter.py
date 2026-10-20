"""Tests for the Claude Code/Vertex Agent Platform Gemma 4 MaaS adapter."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.claude_code_vertex_gemini.adapter import MODEL_ALIAS_ENV
from adapters.claude_code_vertex_gemma4 import adapter
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


@pytest.fixture
def captured(monkeypatch):
    captured: dict = {}

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        captured["home_kind"] = kwargs["home_kind"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    class FakeBridge:
        base_url = "http://127.0.0.1:12345"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path, reasoning_effort):
            captured["bridge_config"] = config
            captured["audit_path"] = audit_path
            captured["reasoning_effort"] = reasoning_effort

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *args, **kwargs: [])
    monkeypatch.setattr(adapter, "AnthropicVertexBridge", FakeBridge)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"type": "result", "result": '{"a": 1}'}), ""
        ),
    )
    monkeypatch.setenv("VERTEX_GEMMA_PROJECT_ID", "test-project")
    monkeypatch.delenv("VERTEX_GEMMA_MODEL", raising=False)
    return captured


def _call_adapter(tmp_path) -> str:
    return adapter._claude_call(
        system_prompt="system",
        user_prompt="user",
        cohort_dir="/cohort",
        data_dictionary_path="/dictionary.xlsx",
        scratch_dir=str(tmp_path / "question" / "scratch"),
        allowed_tools="Read,Glob,Grep,Bash",
        stage="analyze",
    )


def test_defaults_to_gemma_26b_maas_without_reasoning_effort(monkeypatch, captured, tmp_path):
    monkeypatch.setenv("VERTEX_GEMINI_REASONING_EFFORT", "high")

    assert _call_adapter(tmp_path) == '{"a": 1}'

    command, env = captured["cmd"], captured["env"]
    config = captured["bridge_config"]
    assert command[command.index("--model") + 1] == "google/gemma-4-26b-a4b-it-maas"
    assert config.model == "google/gemma-4-26b-a4b-it-maas"
    assert config.project_id == "test-project"
    assert config.location == "global"
    assert config.max_output_tokens == 16_384
    assert captured["reasoning_effort"] is None
    assert captured["home_kind"] == "claude_vertex"
    assert captured["audit_path"] == (
        tmp_path / "question" / "adapter_audit" / "vertex_gemma_bridge.analyze.jsonl"
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "bridge-token"
    for name in MODEL_ALIAS_ENV:
        assert env[name] == "google/gemma-4-26b-a4b-it-maas"


def test_project_is_required(monkeypatch, captured, tmp_path):
    monkeypatch.delenv("VERTEX_GEMMA_PROJECT_ID")
    with pytest.raises(adapter.ClaudeInvocationError, match="VERTEX_GEMMA_PROJECT_ID"):
        _call_adapter(tmp_path)
    assert "cmd" not in captured


def test_adapter_is_registered_and_provenance_records_vertex_settings():
    command = "bash adapters/claude_code_vertex_gemma4/run.sh"
    assert require_supported_adapter(command) == "claude_code_vertex_gemma4"

    provenance = _agent_provenance(
        command,
        environ={
            "VERTEX_GEMMA_PROJECT_ID": "test-project",
            "VERTEX_GEMINI_MODEL": "unrelated",
        },
    )

    assert provenance["adapter"] == "claude_code_vertex_gemma4"
    assert provenance["client"] == "claude_code"
    assert provenance["provider"] == "google_vertex_agent_platform"
    assert provenance["model"] == "google/gemma-4-26b-a4b-it-maas"
    assert provenance["model_source"] == "adapter_default"
    assert provenance["effort_supported"] is False
    assert provenance["project_id"] == "test-project"
    assert provenance["region"] == "global"
    assert provenance["environment"] == {"VERTEX_GEMMA_PROJECT_ID": "test-project"}
