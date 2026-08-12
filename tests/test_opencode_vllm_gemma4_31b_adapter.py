"""Tests for the OpenCode/local-vLLM Gemma 4 31B adapter."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.opencode_vllm_gemma4_31b import adapter
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


def _event(event_type: str, **part) -> str:
    return json.dumps({"type": event_type, "part": part})


def test_base_url_adds_v1_and_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000")
    assert adapter._base_url() == "http://vllm.internal:8000/v1"

    monkeypatch.setenv("VLLM_BASE_URL", "https://vllm.internal/openai/v1/")
    assert adapter._base_url() == "https://vllm.internal/openai/v1"

    monkeypatch.setenv("VLLM_BASE_URL", "http://user:secret@vllm.internal:8000")
    with pytest.raises(RuntimeError, match="VLLM_TOKEN"):
        adapter._base_url()


def test_final_text_uses_last_opencode_assistant_step():
    stdout = "\n".join([
        _event("step_start", type="step-start"),
        _event("text", type="text", text="I will inspect the data."),
        _event("step_finish", type="step-finish", reason="tool-calls"),
        _event("step_start", type="step-start"),
        _event("text", type="text", text='{"classification":'),
        _event("text", type="text", text='"ambiguous"}'),
        _event("step_finish", type="step-finish", reason="stop"),
    ])

    assert adapter._final_text_from_events(stdout) == '{"classification":"ambiguous"}'
    assert adapter._final_text_from_events("plain fallback") == "plain fallback"


def test_opencode_call_uses_disposable_config_and_bridge(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000")
    monkeypatch.setenv("VLLM_MODEL", "test-gemma")
    monkeypatch.setenv("VLLM_TOKEN", "upstream-secret")
    monkeypatch.setenv("OPENCODE_AGENT", "build")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")

    class FakeBridge:
        base_url = "http://127.0.0.1:12345/v1"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path):
            captured["bridge_config"] = config
            captured["audit_path"] = audit_path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        captured["home_kind"] = kwargs["home_kind"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    stdout = "\n".join([
        _event("step_start", type="step-start"),
        _event("text", type="text", text='{"classification":"ambiguous"}'),
        _event("step_finish", type="step-finish", reason="stop"),
    ])

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "VLLMResponsesBridge", FakeBridge)
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result = adapter._opencode_call(
        prompt="benchmark prompt",
        question={
            "question_id": "cohort-Q1",
            "stage": "classify",
            "scratch_dir": str(scratch),
            "cohort_dir": str(cohort),
            "data_dictionary_path": str(dictionary),
        },
    )

    command = captured["cmd"]
    inline_config = json.loads(captured["env"][adapter.CONFIG_ENV])
    provider = inline_config["provider"][adapter.PROVIDER_NAME]
    assert result == '{"classification":"ambiguous"}'
    assert command[:2] == ["opencode", "run"]
    assert "--pure" in command
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--model") + 1] == (
        f"{adapter.PROVIDER_NAME}/test-gemma"
    )
    assert command[command.index("--dir") + 1] == "/work"
    assert "benchmark prompt" not in command
    assert captured["input"] == "benchmark prompt"
    assert captured["home_kind"] == "opencode_vllm"
    assert captured["bridge_config"].api_key == "upstream-secret"
    assert adapter.TOKEN_ENV not in captured["env"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"][adapter.BRIDGE_TOKEN_ENV] == "bridge-token"
    assert provider["npm"] == "@ai-sdk/openai"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:12345/v1"
    assert provider["options"]["apiKey"] == (
        f"{{env:{adapter.BRIDGE_TOKEN_ENV}}}"
    )
    assert inline_config["enabled_providers"] == [adapter.PROVIDER_NAME]
    assert inline_config["permission"]["*"] == "allow"
    assert inline_config["permission"]["task"] == "deny"
    assert inline_config["permission"]["webfetch"] == "deny"
    assert inline_config["share"] == "disabled"
    assert inline_config["snapshot"] is False


def test_adapter_is_registered_and_provenance_excludes_tokens():
    command = "bash adapters/opencode_vllm_gemma4_31b/run.sh"
    assert require_supported_adapter(command) == "opencode_vllm_gemma4_31b"

    provenance = _agent_provenance(
        command,
        environ={
            "VLLM_BASE_URL": "http://vllm.internal:8000",
            "VLLM_MODEL": "test-gemma",
            "VLLM_TOKEN": "must-not-appear",
            "OPENCODE_AGENT": "build",
            "OPENCODE_MAX_OUTPUT_TOKENS": "16384",
            "OPENCODE_CONFIG_CONTENT": "must-not-appear-either",
        },
    )

    assert provenance["adapter"] == "opencode_vllm_gemma4_31b"
    assert provenance["client"] == "opencode"
    assert provenance["provider"] == adapter.PROVIDER_NAME
    assert provenance["model"] == "test-gemma"
    assert provenance["base_url"] == "http://vllm.internal:8000/v1"
    assert provenance["wire_api"] == "responses"
    assert provenance["effort_supported"] is False
    assert provenance["max_output_tokens"] == 16384
    assert provenance["agent_profile"] == "build"
    assert provenance["environment"] == {
        "VLLM_BASE_URL": "http://vllm.internal:8000",
        "VLLM_MODEL": "test-gemma",
        "OPENCODE_AGENT": "build",
        "OPENCODE_MAX_OUTPUT_TOKENS": "16384",
    }
    assert "must-not-appear" not in str(provenance)
