"""Tests for the OpenCode/Azure OpenAI GPT adapter and its credential bridge."""

from __future__ import annotations

import http.client
import io
import json
import subprocess
import urllib.error
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from adapters.opencode_azure_gpt import adapter, azure_bridge
from adapters.opencode_azure_gpt.azure_bridge import (
    AzureResponsesBridge,
    AzureTokenSource,
    BridgeConfig,
)
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


ENDPOINT = "https://example-resource.openai.azure.com/openai/v1"


def _event(event_type: str, **part) -> str:
    return json.dumps({"type": event_type, "part": part})


def test_endpoint_adds_openai_v1_and_rejects_unsafe_values(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example-resource.openai.azure.com/")
    assert adapter._endpoint() == ENDPOINT

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", ENDPOINT + "/")
    assert adapter._endpoint() == ENDPOINT

    for bad in ("http://example.openai.azure.com", "https://user:pw@example.com"):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", bad)
        with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
            adapter._endpoint()

    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    with pytest.raises(RuntimeError, match="must be set"):
        adapter._endpoint()


def test_model_is_required_and_effort_is_validated(monkeypatch):
    monkeypatch.delenv("OPENCODE_AZURE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="OPENCODE_AZURE_MODEL"):
        adapter._model()

    monkeypatch.setenv("OPENCODE_AZURE_REASONING_EFFORT", "XHigh")
    assert adapter._reasoning_effort() == "xhigh"
    monkeypatch.setenv("OPENCODE_AZURE_REASONING_EFFORT", "max")
    with pytest.raises(RuntimeError, match="OPENCODE_AZURE_REASONING_EFFORT"):
        adapter._reasoning_effort()


def test_opencode_call_uses_bridge_and_forces_reasoning(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("OPENCODE_AZURE_MODEL", "gpt-6-luna")
    monkeypatch.setenv("OPENCODE_AZURE_REASONING_EFFORT", "xhigh")

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
    monkeypatch.setattr(adapter, "AzureResponsesBridge", FakeBridge)
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
    env = captured["env"]
    inline_config = json.loads(env[adapter.CONFIG_ENV])
    provider = inline_config["provider"][adapter.PROVIDER_NAME]
    model = provider["models"]["gpt-6-luna"]
    assert result == '{"classification":"ambiguous"}'
    assert command[command.index("--model") + 1] == (
        f"{adapter.PROVIDER_NAME}/gpt-6-luna"
    )
    assert "--pure" in command
    assert "benchmark prompt" not in command
    assert captured["input"] == "benchmark prompt"
    assert captured["home_kind"] == "opencode_azure"
    assert captured["bridge_config"].endpoint == ENDPOINT
    assert captured["bridge_config"].model == "gpt-6-luna"
    assert "AZURE_OPENAI_API_KEY" not in env
    assert "AZURE_OPENAI_ENDPOINT" not in env
    assert "azure-secret" not in json.dumps(env)
    assert env[adapter.BRIDGE_TOKEN_ENV] == "bridge-token"
    assert env[adapter.OUTPUT_TOKEN_MAX_ENV] == "128000"
    assert provider["npm"] == "@ai-sdk/openai"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:12345/v1"
    assert provider["options"]["apiKey"] == f"{{env:{adapter.BRIDGE_TOKEN_ENV}}}"
    assert model["limit"] == {"context": 272_000, "output": 128_000}
    assert model["temperature"] is False
    assert model["options"] == {
        "forceReasoning": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoningSummary": "auto",
        "reasoningEffort": "xhigh",
    }
    assert inline_config["enabled_providers"] == [adapter.PROVIDER_NAME]
    assert inline_config["permission"]["webfetch"] == "deny"
    assert inline_config["permission"]["websearch"] == "deny"


def test_adapter_is_registered_and_provenance_excludes_tokens():
    command = "bash adapters/opencode_azure_gpt/run.sh"
    assert require_supported_adapter(command) == "opencode_azure_gpt"

    provenance = _agent_provenance(
        command,
        environ={
            "AZURE_OPENAI_ENDPOINT": "https://example-resource.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "must-not-appear",
            "OPENCODE_AZURE_MODEL": "gpt-6-luna",
            "OPENCODE_AZURE_REASONING_EFFORT": "xhigh",
            "OPENCODE_CONFIG_CONTENT": "must-not-appear-either",
        },
    )

    assert provenance["adapter"] == "opencode_azure_gpt"
    assert provenance["client"] == "opencode"
    assert provenance["provider"] == "azure"
    assert provenance["model"] == "gpt-6-luna"
    assert provenance["effort_level"] == "xhigh"
    assert provenance["effort_source"] == "OPENCODE_AZURE_REASONING_EFFORT"
    assert provenance["base_url"] == ENDPOINT
    assert provenance["authentication_boundary"] == "trusted_local_bridge"
    assert provenance["environment"] == {
        "OPENCODE_AZURE_MODEL": "gpt-6-luna",
        "OPENCODE_AZURE_REASONING_EFFORT": "xhigh",
    }
    assert "must-not-appear" not in str(provenance)


# --- credential bridge ---------------------------------------------------


class _FakeTokenSource:
    def __init__(self):
        self.issued = 0

    def token(self) -> str:
        self.issued += 1
        return f"token-{self.issued}"

    def invalidate(self) -> None:
        return None


class _FakeUpstream(io.BytesIO):
    def __init__(self, body: bytes, content_type: str):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Type": content_type, "x-request-id": "req-1"}


_COMPLETED = {
    "type": "response.completed",
    "response": {
        "status": "completed",
        "model": "gpt-6-luna",
        "reasoning": {"effort": "xhigh"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "output": [{"type": "message"}],
    },
}
_SSE_BODY = (
    b'event: response.created\ndata: {"type":"response.created"}\n\n'
    + b"event: response.completed\ndata: "
    + json.dumps(_COMPLETED).encode()
    + b"\n\n"
)


def _scripted_urlopen(monkeypatch, outcomes):
    """Replace urlopen with scripted outcomes: ints raise HTTPError."""
    seen: list[tuple[dict, dict]] = []
    sleeps: list[float] = []
    queue = list(outcomes)

    def fake_urlopen(request, timeout):
        seen.append((dict(request.header_items()), json.loads(request.data)))
        outcome = queue.pop(0)
        if isinstance(outcome, tuple):
            status, headers, body = outcome
            raise urllib.error.HTTPError(
                request.full_url, status, "error", headers, io.BytesIO(body)
            )
        return outcome

    monkeypatch.setattr(azure_bridge.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(azure_bridge.time, "sleep", sleeps.append)
    return seen, sleeps


def _post(bridge: AzureResponsesBridge, body: dict, *, token: str | None = None):
    parsed = urlsplit(bridge.base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    connection.request(
        "POST",
        parsed.path + "/responses",
        body=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token or bridge.bearer_token}",
        },
    )
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def _audit(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _bridge(tmp_path, tokens):
    return AzureResponsesBridge(
        BridgeConfig(endpoint=ENDPOINT, model="gpt-6-luna"),
        audit_path=tmp_path / "audit.jsonl",
        token_source=tokens,
    )


def test_bridge_streams_through_with_azure_token_and_audits_settings(
    tmp_path, monkeypatch
):
    seen, _ = _scripted_urlopen(
        monkeypatch, [_FakeUpstream(_SSE_BODY, "text/event-stream")]
    )
    request_body = {
        "model": "something-else",
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "input": [{"role": "user", "content": "patient data must not be audited"}],
        "tools": [{"type": "function", "name": "bash"}],
    }
    with _bridge(tmp_path, _FakeTokenSource()) as bridge:
        assert _post(bridge, request_body, token="wrong")[0] == 401
        status, payload = _post(bridge, request_body)

    assert status == 200
    assert payload == _SSE_BODY
    headers, forwarded = seen[0]
    assert headers["Authorization"] == "Bearer token-1"
    assert forwarded["model"] == "gpt-6-luna"
    assert forwarded["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert forwarded["input"] == request_body["input"]

    audit_text = (tmp_path / "audit.jsonl").read_text()
    assert "patient data" not in audit_text
    assert "token-1" not in audit_text
    records = {record["event"]: record for record in _audit(tmp_path / "audit.jsonl")}
    assert records["request"]["requested_model"] == "something-else"
    assert records["request"]["reasoning_effort"] == "xhigh"
    assert records["request"]["store"] is False
    assert records["response"]["terminal_event"] == "response.completed"
    assert records["response"]["effective_reasoning_effort"] == "xhigh"
    assert records["response"]["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_bridge_retries_401_with_fresh_token_and_429_after_delay(
    tmp_path, monkeypatch
):
    seen, sleeps = _scripted_urlopen(
        monkeypatch,
        [
            (401, {}, b'{"error":{"message":"expired"}}'),
            (429, {"retry-after-ms": "2500"}, b'{"error":{"message":"busy"}}'),
            _FakeUpstream(b'{"status":"completed","usage":{}}', "application/json"),
        ],
    )
    with _bridge(tmp_path, _FakeTokenSource()) as bridge:
        status, payload = _post(bridge, {"input": [], "stream": False})

    assert status == 200
    assert json.loads(payload)["status"] == "completed"
    assert [headers["Authorization"] for headers, _ in seen] == [
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-3",
    ]
    # No wait after the 401; the 429 waits at least its Retry-After hint.
    assert len(sleeps) == 1 and sleeps[0] >= 2.5
    attempts = [r for r in _audit(tmp_path / "audit.jsonl") if r["event"] == "upstream_attempt"]
    assert [(r["status"], r.get("retryable")) for r in attempts] == [
        (401, True),
        (429, True),
        (200, None),
    ]


def test_bridge_relays_non_retryable_error_unchanged(tmp_path, monkeypatch):
    error_body = b'{"error":{"code":"invalid_request","message":"bad input"}}'
    seen, sleeps = _scripted_urlopen(
        monkeypatch, [(400, {"Content-Type": "application/json"}, error_body)]
    )
    with _bridge(tmp_path, _FakeTokenSource()) as bridge:
        status, payload = _post(bridge, {"input": []})

    assert status == 400
    assert payload == error_body
    assert len(seen) == 1 and sleeps == []


def test_token_source_reuses_token_and_hides_stdout_on_failure():
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 0, "tok\n", "")
        return subprocess.CompletedProcess(cmd, 1, "partial-secret", "login required")

    source = AzureTokenSource(reuse_seconds=3600, runner=runner)
    assert source.token() == "tok"
    assert source.token() == "tok"
    assert len(calls) == 1
    assert "https://cognitiveservices.azure.com/" in calls[0][3]

    source.invalidate()
    with pytest.raises(azure_bridge.BridgeError) as excinfo:
        source.token()
    assert "login required" in str(excinfo.value)
    assert "partial-secret" not in str(excinfo.value)
