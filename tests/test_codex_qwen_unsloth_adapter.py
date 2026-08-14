"""Tests for the Codex/Qwen 3.6 local-Unsloth adapter."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import (
    _agent_provenance,
    _timeout_config_for_adapter,
)
from clin_genomic_analysis_benchmark.config import TimeoutConfig


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = (
    REPO_ROOT
    / "adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/adapter.py"
)
SPEC = importlib.util.spec_from_file_location("codex_qwen_unsloth_adapter", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)

from unsloth_bridge import (  # noqa: E402
    BridgeConfig,
    UnslothResponsesBridge,
    UnslothResponsesClient,
)


def test_base_url_adds_v1_and_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("UNSLOTH_STUDIO_BASE_URL", "http://127.0.0.1:8888")
    assert adapter._base_url() == "http://127.0.0.1:8888/v1"

    monkeypatch.setenv(
        "UNSLOTH_STUDIO_BASE_URL", "https://studio.internal/openai/v1/"
    )
    assert adapter._base_url() == "https://studio.internal/openai/v1"

    monkeypatch.setenv(
        "UNSLOTH_STUDIO_BASE_URL", "http://user:secret@127.0.0.1:8888"
    )
    with pytest.raises(RuntimeError, match=adapter.TOKEN_ENV):
        adapter._base_url()


def test_studio_token_is_required_and_supports_legacy_fallback(monkeypatch):
    monkeypatch.delenv(adapter.TOKEN_ENV, raising=False)
    monkeypatch.delenv(adapter.FALLBACK_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match="requires authentication"):
        adapter._studio_token()

    monkeypatch.setenv(adapter.FALLBACK_TOKEN_ENV, "fallback-token")
    assert adapter._studio_token() == "fallback-token"
    monkeypatch.setenv(adapter.TOKEN_ENV, "preferred-token")
    assert adapter._studio_token() == "preferred-token"


def test_unsloth_adapter_uses_longer_request_and_stage_timeouts(monkeypatch):
    monkeypatch.setenv(adapter.TOKEN_ENV, "test-token")
    monkeypatch.delenv("UNSLOTH_REQUEST_TIMEOUT_SECONDS", raising=False)

    assert adapter._bridge_config().request_timeout_seconds == 1_200
    assert _timeout_config_for_adapter(
        "codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl"
    ) == TimeoutConfig(classify=3_600, disambiguate=3_600, analyze=7_200)

    monkeypatch.setenv("UNSLOTH_REQUEST_TIMEOUT_SECONDS", "42")
    assert adapter._bridge_config().request_timeout_seconds == 42


def test_all_stages_use_completion_guards_and_output_schemas(monkeypatch):
    classify = {
        "stage": "classify",
        "disambiguation_concept_menu": [],
    }
    classify_schema = adapter._stage_output_schema(classify)
    assert classify_schema["properties"]["classification"]["enum"] == [
        "ambiguous",
        "unambiguous",
    ]

    disambiguate = {
        "stage": "disambiguate",
        "disambiguation_concept_menu": [
            {"id": "TIME_ORIGIN", "label": "Time origin"},
            {"id": "ENDPOINT", "label": "Endpoint"},
        ],
    }
    disambiguate_schema = adapter._stage_output_schema(disambiguate)
    assert disambiguate_schema["properties"]["concept_ids"]["items"]["enum"] == [
        "TIME_ORIGIN",
        "ENDPOINT",
    ]

    analyze = {"stage": "analyze"}
    analyze_schema = adapter._stage_output_schema(analyze)
    assert analyze_schema["required"] == ["answer_type", "answer"]
    assert "pvalue" in analyze_schema["properties"]["answer_type"]["enum"]

    monkeypatch.setattr(adapter, "_build_prompt", lambda question: "base prompt")
    analyze_prompt = adapter._build_unsloth_prompt({"stage": "analyze"})
    assert "analysis completion guard" in analyze_prompt
    assert "Never end a turn by announcing" in analyze_prompt


def test_same_model_contract_fallback_uses_no_tools_and_returns_json(
    tmp_path, monkeypatch
):
    captured: dict = {}
    monkeypatch.setenv(adapter.TOKEN_ENV, "test-token")

    class FakeClient:
        def __init__(self, config, audit):
            captured["config"] = config
            captured["audit"] = audit

        def complete(self, payload):
            captured["payload"] = payload
            return {
                "output": [{
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": '{"concept_ids":["TIME_ORIGIN"]}',
                    }],
                }],
            }

    monkeypatch.setattr(adapter, "UnslothResponsesClient", FakeClient)
    result = adapter._direct_contract_result({
        "stage": "disambiguate",
        "question_text": "Compare survival from an unspecified origin.",
        "instructions": "Select unresolved concept IDs.",
        "scratch_dir": str(tmp_path / "question" / "scratch"),
        "disambiguation_concept_menu": [{
            "id": "TIME_ORIGIN",
            "label": "Time origin",
            "description": "Where follow-up starts.",
        }],
    })

    assert result == {"concept_ids": ["TIME_ORIGIN"]}
    assert captured["payload"]["tools"] == []
    assert captured["payload"]["reasoning"] == {"effort": "none"}
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert captured["payload"]["text"]["format"]["strict"] is True
    audit = (
        tmp_path
        / "question"
        / "adapter_audit"
        / "unsloth_contract_fallback.disambiguate.jsonl"
    ).read_text()
    assert "TIME_ORIGIN" not in audit
    assert '"valid_contract_json": true' in audit


def test_response_output_text_ignores_reasoning_items():
    response = {
        "output": [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "I will inspect."}],
            },
            {
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": '{"classification":"ambiguous","rationale":"x"}',
                }],
            },
        ]
    }

    assert adapter._response_output_text(response) == (
        '{"classification":"ambiguous","rationale":"x"}'
    )


def test_analyze_contract_aliases_are_normalized_without_changing_values():
    result = adapter._normalize_analyze_result({
        "answer_type": "hazard_ratio",
        "answer": {
            "hazard_ratio": 0.6511,
            "ci_lower": 0.2316,
            "ci_upper": 1.8302,
            "p_value": 0.415778,
        },
        "methods": "Cox model",
    })

    assert result["answer_type"] == "hazard_ratio_with_ci"
    assert result["answer"]["value"] == 0.6511
    assert result["answer"]["ci_low"] == 0.2316
    assert result["answer"]["ci_high"] == 1.8302
    assert result["answer"]["p_value"] == 0.415778
    assert result["methods"] == "Cox model"
    assert adapter.validate_result(result, "analyze") == []

    median = adapter._normalize_analyze_result({
        "answer_type": "median_with_ci",
        "answer": {
            "median_months": 21.02,
            "ci_lower_months": 14.51,
            "ci_upper_months": 39.18,
            "n_patients": 46,
            "n_events": 25,
        },
    })
    assert median["answer"]["value"] == 21.02
    assert median["answer"]["ci_low"] == 14.51
    assert median["answer"]["ci_high"] == 39.18
    assert median["answer"]["n_total"] == 46
    assert adapter.validate_result(median, "analyze") == []


def test_invocation_failure_uses_same_model_contract_fallback(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    question = {
        "stage": "classify",
        "scratch_dir": str(tmp_path / "scratch"),
    }
    monkeypatch.setattr(adapter, "_load_question", lambda path: question)
    monkeypatch.setattr(
        adapter,
        "_codex_call",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("schema failure")),
    )
    monkeypatch.setattr(
        adapter,
        "_direct_contract_result",
        lambda payload: {
            "classification": "ambiguous",
            "rationale": "The endpoint is unspecified.",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adapter.py",
            "--question-file",
            str(tmp_path / "question.json"),
            "--output",
            str(output),
        ],
    )

    assert adapter.main() == 0
    assert json.loads(output.read_text())["classification"] == "ambiguous"


def test_analyze_continues_same_model_from_scratch_after_planning_final(
    tmp_path, monkeypatch
):
    output = tmp_path / "result.json"
    question = {
        "stage": "analyze",
        "scratch_dir": str(tmp_path / "scratch"),
        "question_text": "How many eligible patients are there?",
        "instructions": "Return the typed benchmark JSON.",
    }
    calls: list[dict] = []

    def fake_codex_call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return "Let me fix and run the final script."
        return '{"answer_type":"count","answer":{"value":7}}'

    monkeypatch.setattr(adapter, "_load_question", lambda path: question)
    monkeypatch.setattr(
        adapter, "_build_unsloth_prompt", lambda payload: "initial prompt"
    )
    monkeypatch.setattr(adapter, "_codex_call", fake_codex_call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adapter.py",
            "--question-file",
            str(tmp_path / "question.json"),
            "--output",
            str(output),
        ],
    )

    assert adapter.main() == 0
    assert json.loads(output.read_text()) == {
        "answer_type": "count",
        "answer": {"value": 7},
    }
    assert len(calls) == 2
    assert calls[1]["call_label"] == "continuation_1"
    assert "Let me fix and run" in calls[1]["prompt"]
    assert "same writable scratch directory" in calls[1]["prompt"]
    assert "Do not restart" in calls[1]["prompt"]
    assert calls[1]["prompt"] != "initial prompt"


def test_codex_call_pins_provider_and_shields_studio_token(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv(adapter.TOKEN_ENV, "real-studio-token")
    monkeypatch.setenv("UNSLOTH_MODEL", "test-qwen")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")

    class FakeBridge:
        base_url = "http://127.0.0.1:12345/v1"
        bearer_token = "ephemeral-bridge-token"

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
    monkeypatch.setattr(adapter, "UnslothResponsesBridge", FakeBridge)
    monkeypatch.setattr(
        adapter, "export_agent_session_audit", lambda *args, **kwargs: []
    )
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
    sandbox_env = captured["env"]
    assert result == '{"classification":"ambiguous"}'
    assert command[command.index("--model") + 1] == "test-qwen"
    assert f'model_provider="{adapter.PROVIDER_NAME}"' in command
    assert f"model_context_window={adapter.MODEL_CONTEXT_WINDOW}" in command
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert command[command.index("--output-schema") + 1] == str(
        adapter.SANDBOX_SCRATCH_DIR / ".codex_output_schema.classify.json"
    )
    schema = json.loads(
        (scratch / ".codex_output_schema.classify.json").read_text()
    )
    assert schema["required"] == ["classification", "rationale"]
    assert any(
        "base_url" in value and "http://127.0.0.1:12345/v1" in value
        for value in command
    )
    assert any("wire_api" in value and "responses" in value for value in command)
    assert any(
        "env_key" in value and adapter.BRIDGE_TOKEN_ENV in value
        for value in command
    )
    assert sandbox_env[adapter.BRIDGE_TOKEN_ENV] == "ephemeral-bridge-token"
    assert adapter.TOKEN_ENV not in sandbox_env
    assert adapter.FALLBACK_TOKEN_ENV not in sandbox_env
    assert "CODEX_REASONING_EFFORT" not in sandbox_env
    assert captured["bridge_config"].api_key == "real-studio-token"
    assert captured["bridge_config"].max_retries == 1
    assert captured["bridge_config"].max_requests == 8
    assert captured["bridge_config"].max_output_tokens == 8_192
    assert captured["home_kind"] == "codex_qwen"


def test_client_caps_upstream_output_tokens(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status = 200
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return b'{"output":[]}'

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = UnslothResponsesClient(
        BridgeConfig(
            base_url="http://unused.invalid/v1",
            model="test-qwen",
            api_key="test-token",
            max_output_tokens=8_192,
        ),
        lambda record: None,
    )

    client.complete({"max_output_tokens": 100_000})

    assert captured["payload"]["max_output_tokens"] == 8_192


def test_bridge_streams_native_structured_tool_call(monkeypatch, tmp_path):
    monkeypatch.setattr(
        UnslothResponsesClient,
        "complete",
        lambda self, payload: {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "model": "test-qwen",
            "output": [{
                "type": "function_call",
                "id": "fc_test",
                "call_id": "call_test",
                "name": "exec_command",
                "arguments": '{"cmd":"ls"}',
                "status": "completed",
            }],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
        },
    )
    config = BridgeConfig(
        base_url="http://unused.invalid/v1",
        model="test-qwen",
        api_key="real-token",
    )
    audit_path = tmp_path / "audit.jsonl"
    with UnslothResponsesBridge(config, audit_path=audit_path) as bridge:
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
    audit = audit_path.read_text()
    assert '"tool_count": 1' in audit
    assert "real-token" not in audit


def test_bridge_rejects_the_real_studio_token_at_its_client_boundary(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        UnslothResponsesClient,
        "complete",
        lambda self, payload: pytest.fail("unauthorized request reached upstream"),
    )
    config = BridgeConfig(
        base_url="http://unused.invalid/v1",
        model="test-qwen",
        api_key="real-token",
    )
    with UnslothResponsesBridge(
        config, audit_path=tmp_path / "audit.jsonl"
    ) as bridge:
        request = urllib.request.Request(
            f"{bridge.base_url}/responses",
            data=b"{}",
            headers={
                "Authorization": "Bearer real-token",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request)
    assert exc_info.value.code == 401


def test_adapter_registration_and_provenance_exclude_credentials():
    adapter_name = "codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl"
    command = f"bash adapters/{adapter_name}/run.sh"
    assert require_supported_adapter(command) == adapter_name

    provenance = _agent_provenance(
        command,
        environ={
            "UNSLOTH_STUDIO_BASE_URL": "http://studio.internal:8888",
            "UNSLOTH_MODEL": "test-qwen",
            "UNSLOTH_MAX_RETRIES": "5",
            "UNSLOTH_STUDIO_AUTH_TOKEN": "must-not-appear",
            "API_TOKEN": "also-secret",
            "CODEX_REASONING_EFFORT": "xhigh",
        },
    )

    assert provenance["adapter"] == adapter_name
    assert provenance["provider"] == adapter.PROVIDER_NAME
    assert provenance["model"] == "test-qwen"
    assert provenance["base_url"] == "http://studio.internal:8888/v1"
    assert provenance["wire_api"] == "responses"
    assert provenance["effort_supported"] is False
    assert provenance["authentication_boundary"] == "trusted_local_bridge"
    assert provenance["environment"] == {
        "UNSLOTH_STUDIO_BASE_URL": "http://studio.internal:8888",
        "UNSLOTH_MODEL": "test-qwen",
        "UNSLOTH_MAX_RETRIES": "5",
    }
    assert "must-not-appear" not in str(provenance)
    assert "also-secret" not in str(provenance)
