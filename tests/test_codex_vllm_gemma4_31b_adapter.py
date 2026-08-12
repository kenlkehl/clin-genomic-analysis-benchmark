"""Tests for the Codex/local-vLLM Gemma 4 31B adapter."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.codex_vllm_gemma4_31b import adapter
from adapters.codex_vllm_gemma4_31b.vllm_bridge import (
    BridgeConfig,
    VLLMResponsesBridge,
    VLLMResponsesClient,
    parse_gemma_tool_calls,
    repair_gemma_tool_calls,
)
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


def test_base_url_adds_v1_and_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000")
    assert adapter._base_url() == "http://vllm.internal:8000/v1"

    monkeypatch.setenv("VLLM_BASE_URL", "https://vllm.internal/openai/v1/")
    assert adapter._base_url() == "https://vllm.internal/openai/v1"

    monkeypatch.setenv("VLLM_BASE_URL", "http://user:secret@vllm.internal:8000")
    with pytest.raises(RuntimeError, match="VLLM_TOKEN"):
        adapter._base_url()


def test_codex_call_pins_vllm_responses_provider(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.internal:8000")
    monkeypatch.setenv("VLLM_MODEL", "test-gemma")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    monkeypatch.delenv("VLLM_TOKEN", raising=False)

    class FakeBridge:
        base_url = "http://127.0.0.1:12345/v1"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path):
            captured["bridge_config"] = config

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

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "VLLMResponsesBridge", FakeBridge)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, '{"classification":"ambiguous"}', ""
        ),
    )

    result = adapter._codex_call(
        prompt="prompt",
        question={
            "stage": "classify",
            "scratch_dir": str(scratch),
            "cohort_dir": str(cohort),
            "data_dictionary_path": str(dictionary),
        },
        last_message_file=scratch / "last-message.txt",
    )

    command = captured["cmd"]
    assert result == '{"classification":"ambiguous"}'
    assert command[command.index("--model") + 1] == "test-gemma"
    assert f'model_provider="{adapter.PROVIDER_NAME}"' in command
    assert f"model_context_window={adapter.MODEL_CONTEXT_WINDOW}" in command
    assert any(
        "base_url" in value and "http://127.0.0.1:12345/v1" in value
        for value in command
    )
    assert any("wire_api" in value and "responses" in value for value in command)
    assert "--ignore-user-config" in command
    assert any(
        "env_key" in value and adapter.BRIDGE_TOKEN_ENV in value
        for value in command
    )
    assert not any("model_reasoning_effort" in value for value in command)
    assert "CODEX_REASONING_EFFORT" not in captured["env"]
    assert captured["env"][adapter.BRIDGE_TOKEN_ENV] == "bridge-token"
    assert captured["bridge_config"].base_url == "http://vllm.internal:8000/v1"
    assert captured["home_kind"] == "codex_vllm"


def test_codex_call_uses_optional_vllm_token(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("VLLM_TOKEN", "test-token")

    class FakeBridge:
        base_url = "http://127.0.0.1:12345/v1"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path):
            captured["bridge_config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "VLLMResponsesBridge", FakeBridge)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "{}", ""),
    )

    adapter._codex_call(
        prompt="prompt",
        question={
            "stage": "classify",
            "scratch_dir": str(scratch),
            "cohort_dir": str(cohort),
            "data_dictionary_path": str(dictionary),
        },
        last_message_file=scratch / "last-message.txt",
    )

    assert captured["bridge_config"].api_key == "test-token"
    assert adapter.TOKEN_ENV not in captured["env"]
    assert captured["env"][adapter.BRIDGE_TOKEN_ENV] == "bridge-token"


def test_gemma_tool_markup_is_repaired_for_function_and_custom_tools():
    raw = (
        'Before <|tool_call>call:exec_command{cmd:<|"|>python -c "print({1: 2})"'
        '<|"|>,timeout_ms:1000}<tool_call|>'
        '<|tool_call>call:apply_patch{input:<|"|>*** Begin Patch<|"|>}'
        '<tool_call|>'
    )
    calls = parse_gemma_tool_calls(raw)
    assert calls == [
        ("exec_command", {"cmd": 'python -c "print({1: 2})"', "timeout_ms": 1000}),
        ("apply_patch", {"input": "*** Begin Patch"}),
    ]
    response, count = repair_gemma_tool_calls(
        {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [{
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": raw}],
            }],
            "usage": {},
        },
        tools=[
            {"type": "function", "name": "exec_command"},
            {"type": "custom", "name": "apply_patch"},
        ],
    )

    assert count == 2
    assert response["output"][0]["content"][0]["text"] == "Before"
    assert response["output"][1]["type"] == "function_call"
    assert json.loads(response["output"][1]["arguments"])["timeout_ms"] == 1000
    assert response["output"][2]["type"] == "custom_tool_call"
    assert response["output"][2]["input"] == "*** Begin Patch"


def test_local_bridge_streams_repaired_tool_call(monkeypatch, tmp_path):
    monkeypatch.setattr(
        VLLMResponsesClient,
        "complete",
        lambda self, payload: {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "model": "gemma4-31b",
            "output": [{
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": '<|tool_call>call:exec_command{cmd:<|"|>ls<|"|>}'
                    "<tool_call|>",
                }],
            }],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
        },
    )
    config = BridgeConfig(base_url="http://unused.invalid/v1", model="gemma4-31b")
    with VLLMResponsesBridge(config, audit_path=tmp_path / "audit.jsonl") as bridge:
        payload = json.dumps({
            "input": "list files",
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command"}],
        }).encode()
        request = urllib.request.Request(
            f"{bridge.base_url}/responses",
            data=payload,
            headers={
                "Authorization": f"Bearer {bridge.bearer_token}",
                "Content-Type": "application/json",
            },
        )
        stream = urllib.request.urlopen(request).read().decode()

    assert "event: response.function_call_arguments.done" in stream
    assert '"arguments":"{\\"cmd\\":\\"ls\\"}"' in stream
    assert "<|tool_call>" not in stream
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"repaired_tool_call_count": 1' in audit


def test_adapter_is_registered_and_provenance_excludes_token():
    command = "bash adapters/codex_vllm_gemma4_31b/run.sh"
    assert require_supported_adapter(command) == "codex_vllm_gemma4_31b"

    provenance = _agent_provenance(
        command,
        environ={
            "VLLM_BASE_URL": "http://vllm.internal:8000",
            "VLLM_MODEL": "test-gemma",
            "VLLM_TOKEN": "must-not-appear",
            "CODEX_REASONING_EFFORT": "xhigh",
        },
    )

    assert provenance["adapter"] == "codex_vllm_gemma4_31b"
    assert provenance["provider"] == adapter.PROVIDER_NAME
    assert provenance["model"] == "test-gemma"
    assert provenance["base_url"] == "http://vllm.internal:8000/v1"
    assert provenance["wire_api"] == "responses"
    assert provenance["effort_supported"] is False
    assert provenance["environment"] == {
        "VLLM_BASE_URL": "http://vllm.internal:8000",
        "VLLM_MODEL": "test-gemma",
    }
    assert "must-not-appear" not in str(provenance)
