"""Tests for the Codex adapter for partner models on Vertex's Responses API."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from adapters.codex_vertex_responses import adapter, responses_bridge
from adapters.codex_vertex_responses.responses_bridge import (
    BridgeError,
    ResponsesBridgeConfig,
    VertexResponsesClient,
    VertexResponsesPassThroughBridge,
    prepare_request,
)

EMBER = "meta/muse-ember-1.0"
# The bridge and the test client share urllib; tests patch only upstream calls.
_REAL_URLOPEN = urllib.request.urlopen


def _config(**overrides):
    return ResponsesBridgeConfig(project_id="test-project", model=EMBER, **overrides)


def test_config_builds_the_per_publisher_responses_url():
    config = _config()
    assert config.model_id == "muse-ember-1.0"
    assert config.responses_url == (
        "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global"
        "/publishers/meta/v1/responses"
    )
    regional = _config(location="us-central1")
    assert regional.responses_url.startswith("https://us-central1-aiplatform.googleapis.com/")


@pytest.mark.parametrize("model", ["muse-ember-1.0", "meta/", "/muse", "a/b/c"])
def test_config_requires_publisher_and_model(model):
    with pytest.raises(ValueError):
        ResponsesBridgeConfig(project_id="test-project", model=model)


def test_prepare_request_rewrites_model_and_keeps_everything_else():
    body = {
        "model": "muse-ember-1.0",
        "input": [{"type": "message", "role": "user", "content": "hi"}],
        "tools": [{"type": "function", "name": "shell", "parameters": {}}],
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
    }
    request, stripped = prepare_request(body, _config())
    assert stripped == []
    assert request == {**body, "model": "muse-ember-1.0"}
    assert body["model"] == "muse-ember-1.0"  # caller's object untouched

    forced, _ = prepare_request({**body, "model": "x", "reasoning": {"summary": "auto"}},
                                _config(reasoning_effort="high"))
    assert forced["model"] == "muse-ember-1.0"
    assert forced["reasoning"] == {"summary": "auto", "effort": "high"}


def test_prepare_request_strips_listed_fields(monkeypatch):
    monkeypatch.setattr(responses_bridge, "UNSUPPORTED_REQUEST_FIELDS", ("prompt_cache_key",))
    request, stripped = prepare_request({"input": "x", "prompt_cache_key": "k"}, _config())
    assert stripped == ["prompt_cache_key"]
    assert "prompt_cache_key" not in request


class _FakeTokenSource:
    def __init__(self):
        self.issued = 0
        self.invalidated = 0

    def get(self):
        self.issued += 1
        return f"token-{self.issued}"

    def invalidate(self):
        self.invalidated += 1


class _FakeUpstream(io.BytesIO):
    status = 200

    def __init__(self, raw: bytes, content_type: str):
        super().__init__(raw)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return None


_SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed",'
    b'"output":[{"type":"message"}],"usage":{"input_tokens":11,"output_tokens":3,'
    b'"input_tokens_details":{"cached_tokens":5},"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
)


def _scripted_urlopen(monkeypatch, outcomes):
    """Answer upstream requests in order: an int is an HTTP error status, bytes a 200 body."""
    seen = []
    remaining = list(outcomes)

    def fake_urlopen(request, timeout=None):
        if request.full_url.startswith("http://127.0.0.1"):
            return _REAL_URLOPEN(request)
        seen.append({
            "url": request.full_url,
            "authorization": request.get_header("Authorization"),
            "body": json.loads(request.data),
        })
        outcome = remaining.pop(0)
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(
                request.full_url, outcome, "error", {},
                io.BytesIO(json.dumps({"error": {"message": f"HTTP {outcome}"}}).encode()),
            )
        content_type = "text/event-stream" if outcome.startswith(b"event:") else "application/json"
        return _FakeUpstream(outcome, content_type)

    monkeypatch.setattr(responses_bridge.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    monkeypatch.setattr(responses_bridge.time, "sleep", sleeps.append)
    return seen, sleeps


def _post(bridge, body, *, token=None):
    request = urllib.request.Request(
        f"{bridge.base_url}/responses",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with _REAL_URLOPEN(request) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def _audit(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_bridge_requires_its_ephemeral_key(monkeypatch, tmp_path):
    seen, _ = _scripted_urlopen(monkeypatch, [])
    with VertexResponsesPassThroughBridge(
        _config(), audit_path=tmp_path / "audit.jsonl", token_source=_FakeTokenSource()
    ) as bridge:
        for token in (None, "wrong-key"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _post(bridge, {"input": "x"}, token=token)
            assert excinfo.value.code == 401
    assert seen == []


def test_bridge_relays_stream_with_fresh_token_and_bare_model(monkeypatch, tmp_path):
    seen, _ = _scripted_urlopen(monkeypatch, [_SSE])
    audit_path = tmp_path / "audit.jsonl"
    with VertexResponsesPassThroughBridge(
        _config(), audit_path=audit_path, token_source=_FakeTokenSource()
    ) as bridge:
        status, content_type, raw = _post(
            bridge,
            {"model": "anything", "input": "secret-question-text", "stream": True,
             "tools": [{"type": "function", "name": "shell"}]},
            token=bridge.bearer_token,
        )

    assert status == 200 and content_type == "text/event-stream"
    assert raw == _SSE  # byte-for-byte pass-through
    assert seen[0]["url"].endswith("/publishers/meta/v1/responses")
    assert seen[0]["authorization"] == "Bearer token-1"
    assert seen[0]["body"]["model"] == "muse-ember-1.0"
    assert seen[0]["body"]["input"] == "secret-question-text"
    records = _audit(audit_path)
    response = next(r for r in records if r["event"] == "response")
    assert response["terminal_event"] == "response.completed"
    assert (response["input_tokens"], response["cached_input_tokens"],
            response["output_tokens"], response["reasoning_tokens"]) == (11, 5, 3, 1)
    request = next(r for r in records if r["event"] == "request")
    assert request["tool_types"] == {"function": 1}
    # The audit is content-free.
    text = audit_path.read_text()
    assert "secret-question-text" not in text and "token-1" not in text
    assert bridge.bearer_token not in text


def test_bridge_relays_non_stream_json(monkeypatch, tmp_path):
    body = json.dumps({"status": "completed", "output": [], "usage": {"input_tokens": 2}}).encode()
    _scripted_urlopen(monkeypatch, [body])
    with VertexResponsesPassThroughBridge(
        _config(), audit_path=tmp_path / "audit.jsonl", token_source=_FakeTokenSource()
    ) as bridge:
        status, content_type, raw = _post(bridge, {"input": "x"}, token=bridge.bearer_token)
    assert (status, content_type, raw) == (200, "application/json", body)


def test_bridge_returns_upstream_rejection_without_retrying(monkeypatch, tmp_path):
    seen, sleeps = _scripted_urlopen(monkeypatch, [400])
    with VertexResponsesPassThroughBridge(
        _config(), audit_path=tmp_path / "audit.jsonl", token_source=_FakeTokenSource()
    ) as bridge:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(bridge, {"input": "x"}, token=bridge.bearer_token)
    assert excinfo.value.code == 400
    assert "HTTP 400" in json.loads(excinfo.value.read())["error"]["message"]
    assert len(seen) == 1 and sleeps == []


def test_client_retries_transient_errors_with_backoff(monkeypatch):
    seen, sleeps = _scripted_urlopen(monkeypatch, [429, 503, _SSE])
    client = VertexResponsesClient(
        _config(retry_base_seconds=2.0), _FakeTokenSource(), lambda record: None
    )
    with client.open({"input": "x"}, request_number=1) as response:
        assert response.read() == _SSE
    assert len(seen) == 3
    assert sleeps == [2.0, 4.0]


def test_client_gives_up_after_max_retries(monkeypatch):
    seen, _ = _scripted_urlopen(monkeypatch, [503, 503])
    client = VertexResponsesClient(_config(max_retries=2), _FakeTokenSource(), lambda r: None)
    with pytest.raises(BridgeError) as excinfo:
        client.open({"input": "x"}, request_number=1)
    assert excinfo.value.status == 502
    assert len(seen) == 2


def test_client_retries_401_once_with_a_fresh_token(monkeypatch):
    seen, sleeps = _scripted_urlopen(monkeypatch, [401, _SSE])
    tokens = _FakeTokenSource()
    client = VertexResponsesClient(_config(), tokens, lambda record: None)
    client.open({"input": "x"}, request_number=1).close()
    assert [s["authorization"] for s in seen] == ["Bearer token-1", "Bearer token-2"]
    assert tokens.invalidated == 1 and sleeps == []


def test_client_fails_on_a_second_401(monkeypatch):
    seen, _ = _scripted_urlopen(monkeypatch, [401, 401, _SSE])
    client = VertexResponsesClient(_config(), _FakeTokenSource(), lambda record: None)
    with pytest.raises(BridgeError) as excinfo:
        client.open({"input": "x"}, request_number=1)
    assert excinfo.value.status == 401
    assert len(seen) == 2


def test_bridge_enforces_its_request_limit(monkeypatch, tmp_path):
    _scripted_urlopen(monkeypatch, [_SSE])
    with VertexResponsesPassThroughBridge(
        _config(max_requests=1), audit_path=tmp_path / "audit.jsonl",
        token_source=_FakeTokenSource(),
    ) as bridge:
        _post(bridge, {"input": "x", "stream": True}, token=bridge.bearer_token)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(bridge, {"input": "x", "stream": True}, token=bridge.bearer_token)
    assert excinfo.value.code == 429


def test_adapter_launches_codex_on_the_bridge(tmp_path, monkeypatch):
    captured: dict = {}
    scratch = tmp_path / "question" / "scratch"
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    monkeypatch.setenv("VERTEX_RESPONSES_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_RESPONSES_MODEL", EMBER)
    monkeypatch.setenv("VERTEX_RESPONSES_REASONING_EFFORT", "high")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    monkeypatch.delenv("VERTEX_RESPONSES_CONTEXT_WINDOW", raising=False)

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
        captured["home_kind"] = kwargs["home_kind"]
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "VertexResponsesPassThroughBridge", FakeBridge)
    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, '{"value": 1}', ""),
    )

    result = adapter._codex_call(
        prompt="prompt",
        question={
            "scratch_dir": str(scratch),
            "cohort_dir": str(cohort),
            "data_dictionary_path": str(dictionary),
        },
        last_message_file=scratch / "last-message.txt",
    )

    command = captured["cmd"]
    assert result == '{"value": 1}'
    assert captured["config"].model == EMBER
    assert captured["config"].reasoning_effort == "high"
    assert command[command.index("--model") + 1] == "muse-ember-1.0"
    assert f"model_context_window={adapter.DEFAULT_CONTEXT_WINDOW}" in command
    assert f'model_provider="{adapter.PROVIDER_NAME}"' in command
    assert 'web_search="disabled"' in command
    assert 'model_reasoning_effort="high"' in command
    assert any("base_url" in value and "127.0.0.1:12345" in value for value in command)
    assert any("wire_api" in value and "responses" in value for value in command)
    assert "--add-dir" not in command
    assert captured["home_kind"] == "codex_vertex"
    assert captured["env"]["VERTEX_RESPONSES_BRIDGE_KEY"] == "bridge-token"
    assert "CODEX_REASONING_EFFORT" not in captured["env"]


def test_adapter_rejects_missing_or_bad_settings(monkeypatch):
    monkeypatch.delenv("VERTEX_RESPONSES_PROJECT_ID", raising=False)
    monkeypatch.setenv("VERTEX_RESPONSES_MODEL", EMBER)
    with pytest.raises(RuntimeError, match="VERTEX_RESPONSES_PROJECT_ID"):
        adapter._bridge_config()
    monkeypatch.setenv("VERTEX_RESPONSES_PROJECT_ID", "p")
    monkeypatch.setenv("VERTEX_RESPONSES_MODEL", "muse-ember-1.0")
    with pytest.raises(RuntimeError, match="publisher"):
        adapter._bridge_config()
    monkeypatch.setenv("VERTEX_RESPONSES_MODEL", EMBER)
    monkeypatch.setenv("VERTEX_RESPONSES_REASONING_EFFORT", "xhigh")
    with pytest.raises(RuntimeError, match="REASONING_EFFORT"):
        adapter._bridge_config()


def test_adapter_is_registered_for_isolation():
    from clin_genomic_analysis_benchmark.agent.isolation import supported_adapter

    assert supported_adapter("bash adapters/codex_vertex_responses/run.sh") == (
        "codex_vertex_responses"
    )


def test_run_provenance_records_the_vertex_model():
    from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance

    provenance = _agent_provenance(
        "bash adapters/codex_vertex_responses/run.sh",
        {
            "VERTEX_RESPONSES_PROJECT_ID": "test-project",
            "VERTEX_RESPONSES_MODEL": EMBER,
            "VERTEX_RESPONSES_MAX_REQUESTS": "300",
            "CODEX_MODEL": "should-not-leak",
        },
    )
    assert provenance["adapter"] == "codex_vertex_responses"
    assert provenance["model"] == EMBER
    assert provenance["project_id"] == "test-project"
    assert provenance["region"] == "global"
    assert provenance["environment"] == {
        "VERTEX_RESPONSES_PROJECT_ID": "test-project",
        "VERTEX_RESPONSES_MODEL": EMBER,
        "VERTEX_RESPONSES_MAX_REQUESTS": "300",
    }
