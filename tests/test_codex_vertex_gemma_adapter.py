"""Tests for the Codex/Vertex Agent Platform Gemma adapter."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

from adapters.codex_vertex_gemma4_26b import adapter
from adapters.codex_vertex_gemma4_26b.vertex_bridge import (
    DEFAULT_MODEL,
    BridgeConfig,
    VertexChatClient,
    VertexResponsesBridge,
    chat_response_to_response,
    response_events,
    responses_request_to_chat,
)


def test_responses_request_translates_messages_and_codex_tools():
    config = BridgeConfig(project_id="test-project")
    request, kinds = responses_request_to_chat(
        {
            "instructions": "Be exact.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "List files"}]},
                {
                    "type": "function_call",
                    "call_id": "call_shell",
                    "name": "shell",
                    "arguments": '{"cmd":"ls"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_shell",
                    "output": "patients.csv",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "Run a command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                },
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Apply a patch",
                    "format": {"type": "grammar"},
                },
            ],
            "max_output_tokens": 99_999,
        },
        config,
    )

    assert request["model"] == DEFAULT_MODEL
    assert request["max_tokens"] == config.max_output_tokens
    assert request["messages"] == [
        {"role": "system", "content": "Be exact."},
        {"role": "user", "content": "List files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_shell",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"cmd":"ls"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_shell", "content": "patients.csv"},
    ]
    assert kinds == {"shell": "function", "apply_patch": "custom"}
    custom = next(
        tool for tool in request["tools"] if tool["function"]["name"] == "apply_patch"
    )
    assert custom["function"]["parameters"]["required"] == ["input"]


def test_chat_answer_translates_to_responses_stream():
    response = chat_response_to_response(
        {
            "id": "chatcmpl_test",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        model=DEFAULT_MODEL,
        tool_kinds={},
    )
    events = response_events(response)

    assert response["output"][0]["content"][0]["text"] == "done"
    assert response["usage"]["total_tokens"] == 12
    assert events[0]["type"] == "response.created"
    assert any(
        event["type"] == "response.output_text.delta" and event["delta"] == "done"
        for event in events
    )
    assert events[-1]["type"] == "response.completed"


def test_chat_custom_tool_call_restores_responses_shape():
    response = chat_response_to_response(
        {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": json.dumps({"input": "*** Begin Patch"}),
                        },
                    }],
                }
            }],
            "usage": {},
        },
        model=DEFAULT_MODEL,
        tool_kinds={"apply_patch": "custom"},
    )

    item = response["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["call_id"] == "call_patch"
    assert item["input"] == "*** Begin Patch"
    event_types = [event["type"] for event in response_events(response)]
    assert "response.custom_tool_call_input.done" in event_types


def test_local_bridge_requires_auth_and_streams(monkeypatch, tmp_path):
    monkeypatch.setattr(
        VertexChatClient,
        "complete",
        lambda self, payload: {
            "id": "chatcmpl_test",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    config = BridgeConfig(project_id="test-project")
    with VertexResponsesBridge(config, audit_path=tmp_path / "audit.jsonl") as bridge:
        payload = json.dumps({"input": "secret-question-text", "stream": True}).encode()
        unauthorized = urllib.request.Request(
            f"{bridge.base_url}/responses",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(unauthorized)
            raise AssertionError("request unexpectedly authorized")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        request = urllib.request.Request(
            f"{bridge.base_url}/responses",
            data=payload,
            headers={
                "Authorization": f"Bearer {bridge.bearer_token}",
                "Content-Type": "application/json",
            },
        )
        stream = urllib.request.urlopen(request).read().decode()

    assert "event: response.output_text.delta" in stream
    assert '"delta":"OK"' in stream
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(record["event"] == "response" for record in records)
    assert "secret-question-text" not in (tmp_path / "audit.jsonl").read_text()


def test_adapter_pins_local_provider_without_reasoning(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("VERTEX_GEMMA_PROJECT_ID", "test-project")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")

    class FakeBridge:
        base_url = "http://127.0.0.1:12345/v1"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path):
            captured["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "VertexResponsesBridge", FakeBridge)
    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
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
    assert command[command.index("--model") + 1] == DEFAULT_MODEL
    assert f"model_context_window={adapter.MODEL_CONTEXT_WINDOW}" in command
    assert f'model_provider="{adapter.PROVIDER_NAME}"' in command
    assert any("wire_api" in value and "responses" in value for value in command)
    assert not any("model_reasoning_effort" in value for value in command)
    assert captured["env"]["VERTEX_GEMMA_BRIDGE_KEY"] == "bridge-token"
    assert "CODEX_REASONING_EFFORT" not in captured["env"]
