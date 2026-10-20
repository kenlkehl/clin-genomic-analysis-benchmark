"""Tests for the Claude Code/Vertex Agent Platform Gemini adapter."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.claude_code_vertex_gemini import adapter
from adapters.claude_code_vertex_gemini.anthropic_bridge import (
    SIGNATURE_PLACEHOLDER,
    AnthropicVertexBridge,
    ThoughtSignatures,
    anthropic_request_to_chat,
    chat_response_to_message,
    message_events,
)
from adapters.codex_vertex_gemma4_26b.vertex_bridge import BridgeConfig, VertexChatClient
from clin_genomic_analysis_benchmark.agent import isolation
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance

CONFIG = BridgeConfig(
    project_id="test-project", model="google/test-gemini", max_output_tokens=1000
)
BASH_TOOL = {
    "name": "Bash",
    "description": "Run a command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


def _gemini_tool_call(signature: str | None = "sig-1") -> dict:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "Bash", "arguments": '{"command":"ls"}'},
    }
    if signature:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def test_request_translates_system_tools_and_tool_results():
    request, placeholders = anthropic_request_to_chat(
        {
            "system": [
                {"type": "text", "text": "Be exact."},
                {"type": "text", "text": "Use tools.", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {"role": "user", "content": "List files"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hmm", "signature": "x"},
                        {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                         "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<system-reminder>note</system-reminder>"},
                        {"type": "tool_result", "tool_use_id": "toolu_a",
                         "content": [{"type": "text", "text": "patients.csv"}]},
                    ],
                },
            ],
            "tools": [BASH_TOOL, {"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "any"},
            "max_tokens": 99_999,
            "temperature": 1,
        },
        CONFIG,
        reasoning_effort="high",
        signatures=ThoughtSignatures(),
    )

    messages = request["messages"]
    assert messages[0] == {"role": "system", "content": "Be exact.\n\nUse tools."}
    assert messages[1] == {"role": "user", "content": "List files"}
    assert messages[2]["tool_calls"][0]["function"] == {
        "name": "Bash", "arguments": '{"command":"ls"}'
    }
    # The tool result must directly follow the call, ahead of same-turn text.
    assert messages[3] == {"role": "tool", "tool_call_id": "toolu_a", "content": "patients.csv"}
    assert messages[4] == {"role": "user", "content": "<system-reminder>note</system-reminder>"}
    assert [tool["function"]["name"] for tool in request["tools"]] == ["Bash"]
    assert request["tools"][0]["function"]["parameters"] == BASH_TOOL["input_schema"]
    assert request["tool_choice"] == "required"
    assert request["max_tokens"] == 1000
    assert request["model"] == "google/test-gemini"
    assert request["reasoning_effort"] == "high"
    assert request["temperature"] == 1
    # The bridge never issued toolu_a, so the first call gets the placeholder.
    assert messages[2]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIGNATURE_PLACEHOLDER}
    }
    assert placeholders == 1


def test_thought_signatures_round_trip_through_tool_use_ids():
    signatures = ThoughtSignatures()
    second_call = {**_gemini_tool_call(None), "id": "call_2"}
    message = chat_response_to_message(
        {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Checking.",
                    "tool_calls": [_gemini_tool_call("sig-1"), second_call],
                },
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "completion_tokens_details": {"reasoning_tokens": 40},
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        },
        model="google/test-gemini",
        signatures=signatures,
    )

    text, first, second = message["content"]
    assert text == {"type": "text", "text": "Checking."}
    assert first["type"] == "tool_use" and first["input"] == {"command": "ls"}
    assert first["id"].startswith("toolu_") and first["id"] != second["id"]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {
        "input_tokens": 40,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 60,
        "output_tokens": 50,
    }

    request, placeholders = anthropic_request_to_chat(
        {"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": message["content"]},
        ]},
        CONFIG,
        reasoning_effort=None,
        signatures=signatures,
    )
    calls = request["messages"][-1]["tool_calls"]
    assert calls[0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    # Gemini signs only the first call of a parallel step; keep it that way.
    assert "extra_content" not in calls[1]
    assert placeholders == 0
    assert "reasoning_effort" not in request


def test_tool_result_errors_and_images_are_preserved():
    request, _ = anthropic_request_to_chat(
        {"messages": [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "is_error": True,
                 "content": "permission denied"},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "AAAA"}},
                ]},
                {"type": "tool_result", "tool_use_id": "toolu_c", "content": ""},
            ],
        }]},
        CONFIG,
        reasoning_effort=None,
        signatures=ThoughtSignatures(),
    )

    first, second, third, images = request["messages"]
    assert first["content"] == "Error: permission denied"
    assert "image attached" in second["content"]
    assert third["content"] == "(no output)"
    assert images == {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}


def test_stop_reasons_and_stream_events():
    signatures = ThoughtSignatures()
    truncated = chat_response_to_message(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
        model="m", signatures=signatures,
    )
    assert truncated["stop_reason"] == "max_tokens"

    message = chat_response_to_message(
        {"choices": [{"finish_reason": "tool_calls",
                      "message": {"tool_calls": [_gemini_tool_call()]}}]},
        model="m", signatures=signatures,
    )
    events = message_events(message)
    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1]["content_block"]["type"] == "tool_use"
    assert json.loads(events[2]["delta"]["partial_json"]) == {"command": "ls"}
    assert events[4]["delta"]["stop_reason"] == "tool_use"


def test_local_bridge_requires_auth_and_streams(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_complete(self, payload):
        seen["payload"] = payload
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(VertexChatClient, "complete", fake_complete)
    with AnthropicVertexBridge(
        CONFIG, audit_path=tmp_path / "audit.jsonl", reasoning_effort="low"
    ) as bridge:
        payload = json.dumps({
            "model": "anything",
            "max_tokens": 10,
            "stream": True,
            "messages": [{"role": "user", "content": "secret-question-text"}],
        }).encode()
        url = f"{bridge.base_url}/v1/messages?beta=true"
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(urllib.request.Request(url, data=payload))
        assert unauthorized.value.code == 401

        stream = urllib.request.urlopen(urllib.request.Request(
            url, data=payload, headers={"Authorization": f"Bearer {bridge.bearer_token}"}
        )).read().decode()
        counted = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{bridge.base_url}/v1/messages/count_tokens",
            data=payload,
            headers={"x-api-key": bridge.bearer_token},
        )).read())

    assert "event: message_start" in stream
    assert '"text":"OK"' in stream
    assert counted["input_tokens"] > 0
    assert seen["payload"]["model"] == "google/test-gemini"
    assert seen["payload"]["reasoning_effort"] == "low"
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "secret-question-text" not in audit
    assert any(json.loads(line)["event"] == "response" for line in audit.splitlines())


def test_bridge_request_limit_fails_without_retryable_status(monkeypatch, tmp_path):
    monkeypatch.setattr(
        VertexChatClient,
        "complete",
        lambda self, payload: {"choices": [{"message": {"content": "OK"}}]},
    )
    config = BridgeConfig(project_id="p", model="google/m", max_requests=1)
    with AnthropicVertexBridge(config, audit_path=tmp_path / "audit.jsonl") as bridge:
        def post():
            return urllib.request.urlopen(urllib.request.Request(
                f"{bridge.base_url}/v1/messages",
                data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"x-api-key": bridge.bearer_token},
            ))
        post()
        with pytest.raises(urllib.error.HTTPError) as exceeded:
            post()
    assert exceeded.value.code == 400
    assert json.loads(exceeded.value.read())["error"]["type"] == "invalid_request_error"


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
    monkeypatch.setenv("VERTEX_GEMINI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", "gemini-test")
    monkeypatch.delenv("VERTEX_GEMINI_REASONING_EFFORT", raising=False)
    return captured


def _call_adapter(tmp_path) -> str:
    return adapter._claude_call(
        system_prompt="system",
        user_prompt="user",
        cohort_dir="/cohort",
        data_dictionary_path="/dictionary.xlsx",
        scratch_dir=str(tmp_path / "question" / "scratch"),
        allowed_tools="Read,Glob,Grep,Bash",
        stage="classify",
    )


def test_claude_call_routes_every_alias_through_the_bridge(monkeypatch, captured, tmp_path):
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY",
                 "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "host-value")
    monkeypatch.setenv("VERTEX_GEMINI_REASONING_EFFORT", "HIGH")

    assert _call_adapter(tmp_path) == '{"a": 1}'

    command, env = captured["cmd"], captured["env"]
    assert command[command.index("--model") + 1] == "google/gemini-test"
    assert captured["home_kind"] == "claude_vertex"
    assert captured["bridge_config"].project_id == "test-project"
    assert captured["bridge_config"].location == "global"
    assert captured["reasoning_effort"] == "high"
    assert captured["audit_path"] == (
        tmp_path / "question" / "adapter_audit" / "vertex_gemini_bridge.classify.jsonl"
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "bridge-token"
    for name in adapter.MODEL_ALIAS_ENV:
        assert env[name] == "google/gemini-test"
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY",
                 "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY"):
        assert name not in env


def test_required_settings_fail_before_subprocess(monkeypatch, captured, tmp_path):
    monkeypatch.delenv("VERTEX_GEMINI_PROJECT_ID")
    with pytest.raises(adapter.ClaudeInvocationError, match="VERTEX_GEMINI_PROJECT_ID"):
        _call_adapter(tmp_path)

    monkeypatch.setenv("VERTEX_GEMINI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_GEMINI_REASONING_EFFORT", "xhigh")
    with pytest.raises(adapter.ClaudeInvocationError, match="VERTEX_GEMINI_REASONING_EFFORT"):
        _call_adapter(tmp_path)
    assert "cmd" not in captured


def test_claude_vertex_home_has_no_cloud_credentials(tmp_path):
    credential = tmp_path / "adc.json"
    credential.write_text("{}")
    home = tmp_path / "home"
    home.mkdir()

    isolation._seed_home(
        home, "claude_vertex", {"GOOGLE_APPLICATION_CREDENTIALS": str(credential)}
    )

    assert (home / ".claude/settings.json").read_text() == "{}"
    assert not (home / ".config/gcloud").exists()


def test_adapter_is_registered_and_provenance_records_vertex_settings():
    command = "bash adapters/claude_code_vertex_gemini/run.sh"
    assert require_supported_adapter(command) == "claude_code_vertex_gemini"

    provenance = _agent_provenance(
        command,
        environ={
            "VERTEX_GEMINI_PROJECT_ID": "test-project",
            "VERTEX_GEMINI_MODEL": "gemini-test",
            "VERTEX_GEMINI_REASONING_EFFORT": "high",
            "CLINGEN_CLAUDE_MODEL": "claude-opus-4-8",
        },
    )

    assert provenance["adapter"] == "claude_code_vertex_gemini"
    assert provenance["client"] == "claude_code"
    assert provenance["provider"] == "google_vertex_agent_platform"
    assert provenance["model"] == "google/gemini-test"
    assert provenance["effort_level"] == "high"
    assert provenance["effort_source"] == "VERTEX_GEMINI_REASONING_EFFORT"
    assert provenance["project_id"] == "test-project"
    assert provenance["region"] == "global"
    assert provenance["wire_api"] == "anthropic_messages"
    assert provenance["environment"] == {
        "VERTEX_GEMINI_PROJECT_ID": "test-project",
        "VERTEX_GEMINI_MODEL": "gemini-test",
        "VERTEX_GEMINI_REASONING_EFFORT": "high",
    }
