"""Tests for the Claude Code adapter on the Databricks AI Gateway."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from adapters.claude_code_databricks import adapter
from adapters.claude_code_databricks.databricks_bridge import (
    BridgeConfig,
    DatabricksAnthropicBridge,
    DatabricksTokenSource,
    canonical_model,
    prepare_request,
)
from clin_genomic_analysis_benchmark.agent import isolation
from clin_genomic_analysis_benchmark.agent.isolation import require_supported_adapter
from clin_genomic_analysis_benchmark.agent.orchestrator import (
    _agent_provenance,
    databricks_canonical_model,
)

ENDPOINT = "ai_gateway_model_services_prod.team_proj.team_proj-claude-haiku-5-5"
CONFIG = BridgeConfig(endpoint=ENDPOINT, model="claude-haiku-5-5", max_requests=3)


class FakeTokens:
    profile = "test-profile"
    host = "https://example.databricks.net"

    def __init__(self):
        self.calls = 0
        self.invalidated = 0

    def headers(self):
        self.calls += 1
        return {"Authorization": f"Bearer upstream-{self.calls}"}

    def invalidate(self):
        self.invalidated += 1


class FakeUpstream(io.BytesIO):
    def __init__(self, status: int, body: bytes, content_type: str):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type, "request-id": "req_1"}

    def getcode(self):
        return self.status


def _bridge(tmp_path, replies, seen):
    """A bridge whose upstream returns `replies` in order and records requests."""
    def opener(request, timeout):
        seen.append({"url": request.full_url, "headers": dict(request.header_items()),
                     "body": json.loads(request.data)})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    return DatabricksAnthropicBridge(CONFIG, audit_path=tmp_path / "audit.jsonl",
                                     token_source=FakeTokens(), opener=opener)


def _post(bridge, body, *, token=None, headers=None):
    request = urllib.request.Request(
        bridge.base_url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token or bridge.bearer_token}",
                 "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read()


def _audit(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


def test_canonical_model_matches_the_orchestrator_copy():
    for endpoint in (ENDPOINT, "x.y.z-claude-opus-5-5", "x-claude-sonnet-5", "no-model-here"):
        assert canonical_model(endpoint) == databricks_canonical_model(endpoint)
    assert canonical_model(ENDPOINT) == "claude-haiku-5-5"


def test_prepare_request_rewrites_model_and_strips_hipaa_rejected_features():
    body = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": []}, "output_config": {"effort": "low"}}
    data, headers, facts = prepare_request(
        body, {"anthropic-beta": "interleaved-thinking-2025-05-14,context-management-2025-06-27"},
        CONFIG)
    sent = json.loads(data)
    assert sent["model"] == ENDPOINT
    assert "context_management" not in sent
    assert headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers
    assert facts["stripped"] == {"body_fields": ["context_management"],
                                 "betas": ["context-management-2025-06-27"]}
    assert facts["effort"] == "low" and "model_mismatch" not in facts
    assert body["model"] == "claude-haiku-5-5"      # caller's body untouched


def test_bridge_passes_a_json_reply_through_and_audits_usage(tmp_path):
    seen: list = []
    reply = {"type": "message", "content": [{"type": "text", "text": "SECRET-CONTENT-7f3"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 7, "output_tokens": 2}}
    with _bridge(tmp_path, [FakeUpstream(200, json.dumps(reply).encode(),
                                         "application/json")], seen) as bridge:
        status, raw = _post(bridge, {"model": "claude-haiku-5-5", "messages": []})
    assert status == 200 and json.loads(raw) == reply
    assert seen[0]["url"] == "https://example.databricks.net/ai-gateway/anthropic/v1/messages"
    assert seen[0]["headers"]["Authorization"] == "Bearer upstream-1"
    assert seen[0]["body"]["model"] == ENDPOINT
    events = _audit(tmp_path)
    response = next(e for e in events if e["event"] == "response")
    assert response["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert events[-1]["usage_totals"] == {"input_tokens": 7, "output_tokens": 2}
    assert "SECRET-CONTENT-7f3" not in (tmp_path / "audit.jsonl").read_text()


def test_bridge_streams_sse_through_and_reads_usage(tmp_path):
    sse = (b'event: message_start\ndata: {"type":"message_start","message":'
           b'{"usage":{"input_tokens":11,"output_tokens":1}}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta",'
           b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
           b'event: message_delta\ndata: {"type":"message_delta","delta":'
           b'{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n')
    with _bridge(tmp_path, [FakeUpstream(200, sse, "text/event-stream")], []) as bridge:
        status, raw = _post(bridge, {"model": "claude-haiku-5-5", "stream": True, "messages": []})
    assert status == 200 and raw == sse
    response = next(e for e in _audit(tmp_path) if e["event"] == "response")
    assert response["usage"] == {"input_tokens": 11, "output_tokens": 5}
    assert response["stop_reason"] == "end_turn"


def test_bridge_retries_a_401_once_with_a_fresh_token(tmp_path):
    seen: list = []
    expired = urllib.error.HTTPError("u", 401, "expired", {}, io.BytesIO(b"{}"))
    ok = FakeUpstream(200, b'{"usage": {}}', "application/json")
    with _bridge(tmp_path, [expired, ok], seen) as bridge:
        status, _ = _post(bridge, {"model": "claude-haiku-5-5", "messages": []})
        assert bridge.tokens.invalidated == 1
    assert status == 200
    assert [s["headers"]["Authorization"] for s in seen] == ["Bearer upstream-1",
                                                             "Bearer upstream-2"]


def test_bridge_passes_upstream_errors_through(tmp_path):
    error = urllib.error.HTTPError(
        "u", 429, "busy", {"Content-Type": "application/json"},
        io.BytesIO(b'{"type":"error","error":{"type":"rate_limit_error"}}'))
    with _bridge(tmp_path, [error], []) as bridge:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(bridge, {"model": "claude-haiku-5-5", "messages": []})
    assert caught.value.code == 429
    response = next(e for e in _audit(tmp_path) if e["event"] == "response")
    assert response["status"] == 429 and "rate_limit_error" in response["error"]


def test_bridge_rejects_a_wrong_key_and_caps_requests(tmp_path):
    replies = [FakeUpstream(200, b'{"usage": {}}', "application/json") for _ in range(3)]
    with _bridge(tmp_path, replies, []) as bridge:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(bridge, {"model": "m", "messages": []}, token="wrong")
        assert caught.value.code == 401
        for _ in range(3):
            _post(bridge, {"model": "claude-haiku-5-5", "messages": []})
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(bridge, {"model": "claude-haiku-5-5", "messages": []})
        assert caught.value.code == 400        # not 429, so Claude Code does not spin


def test_token_source_caches_until_near_expiry_and_reports_cli_failure(monkeypatch):
    calls: list = []

    def runner(command):
        calls.append(command)
        return json.dumps({"access_token": f"t{len(calls)}", "expires_in": 3600})

    tokens = DatabricksTokenSource(profile="p", runner=runner, host="https://h")
    assert tokens.headers() == {"Authorization": "Bearer t1"}
    assert tokens.headers() == {"Authorization": "Bearer t1"}
    assert calls == [["databricks", "auth", "token", "--profile", "p"]]
    tokens.invalidate()
    assert tokens.headers() == {"Authorization": "Bearer t2"}

    bad = DatabricksTokenSource(profile="p", runner=lambda c: "not json", host="https://h")
    with pytest.raises(Exception, match="no access token"):
        bad.headers()


@pytest.fixture
def captured(monkeypatch):
    captured: dict = {}

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["environment"]
        captured["home_kind"] = kwargs["home_kind"]

        class Launch:
            command = cmd
            environment = kwargs["environment"]
        yield Launch()

    class FakeBridge:
        base_url = "http://127.0.0.1:12345"
        bearer_token = "bridge-token"

        def __init__(self, config, *, audit_path):
            captured["bridge_config"] = config
            captured["audit_path"] = audit_path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(adapter, "export_agent_session_audit", lambda *a, **k: [])
    monkeypatch.setattr(adapter, "DatabricksAnthropicBridge", FakeBridge)
    monkeypatch.setattr(adapter.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(
        cmd, 0, json.dumps({"type": "result", "result": '{"a": 1}'}), ""))
    monkeypatch.setenv("DATABRICKS_CLAUDE_ENDPOINT", ENDPOINT)
    for name in ("CLINGEN_CLAUDE_MODEL", "CLINGEN_CLAUDE_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    return captured


def _call_adapter(tmp_path) -> str:
    return adapter._claude_call(
        system_prompt="system", user_prompt="user", cohort_dir="/cohort",
        data_dictionary_path="/dictionary.xlsx",
        scratch_dir=str(tmp_path / "question" / "scratch"),
        allowed_tools="Read,Glob,Grep,Bash")


def test_claude_call_routes_every_alias_through_the_bridge(monkeypatch, captured, tmp_path):
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY",
                 "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_MODEL"):
        monkeypatch.setenv(name, "host-value")
    monkeypatch.setenv("CLINGEN_CLAUDE_EFFORT", "High")

    assert _call_adapter(tmp_path) == '{"a": 1}'

    command, env = captured["cmd"], captured["env"]
    assert command[command.index("--model") + 1] == "claude-haiku-5-5"
    assert command[command.index("--effort") + 1] == "high"
    assert command[-1] == "user"
    assert captured["home_kind"] == "claude_bridge"
    assert captured["bridge_config"].endpoint == ENDPOINT
    assert captured["audit_path"] == (
        tmp_path / "question" / "adapter_audit" / "databricks_bridge.analyze.jsonl")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "bridge-token"
    for name in adapter.MODEL_ALIAS_ENV:
        assert env[name] == "claude-haiku-5-5"
    for name in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY",
                 "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_MODEL"):
        assert name not in env


def test_required_settings_fail_before_subprocess(monkeypatch, captured, tmp_path):
    monkeypatch.delenv("DATABRICKS_CLAUDE_ENDPOINT")
    with pytest.raises(adapter.ClaudeInvocationError, match="DATABRICKS_CLAUDE_ENDPOINT"):
        _call_adapter(tmp_path)
    monkeypatch.setenv("DATABRICKS_CLAUDE_ENDPOINT", "endpoint-without-a-model-id")
    with pytest.raises(adapter.ClaudeInvocationError, match="CLINGEN_CLAUDE_MODEL"):
        _call_adapter(tmp_path)
    monkeypatch.setenv("DATABRICKS_CLAUDE_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("CLINGEN_CLAUDE_EFFORT", "extreme")
    with pytest.raises(adapter.ClaudeInvocationError, match="CLINGEN_CLAUDE_EFFORT"):
        _call_adapter(tmp_path)
    assert "cmd" not in captured


def test_bridge_home_has_no_cloud_credentials(tmp_path):
    credential = tmp_path / "adc.json"
    credential.write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    isolation._seed_home(home, "claude_bridge", {"GOOGLE_APPLICATION_CREDENTIALS": str(credential)})
    assert (home / ".claude/settings.json").read_text() == "{}"
    assert not (home / ".config/gcloud").exists()


def test_adapter_is_registered_and_provenance_records_gateway_settings():
    command = "bash adapters/claude_code_databricks/run.sh"
    assert require_supported_adapter(command) == "claude_code_databricks"
    provenance = _agent_provenance(command, environ={
        "DATABRICKS_CLAUDE_ENDPOINT": ENDPOINT,
        "CLINGEN_CLAUDE_EFFORT": "medium",
        "VERTEX_GEMINI_MODEL": "unrelated",
    })
    assert provenance["adapter"] == "claude_code_databricks"
    assert provenance["provider"] == "databricks_ai_gateway"
    assert provenance["model"] == "claude-haiku-5-5"
    assert provenance["model_source"] == "DATABRICKS_CLAUDE_ENDPOINT"
    assert provenance["gateway_endpoint"] == ENDPOINT
    assert provenance["effort_level"] == "medium"
    assert provenance["environment"] == {"DATABRICKS_CLAUDE_ENDPOINT": ENDPOINT,
                                         "CLINGEN_CLAUDE_EFFORT": "medium"}
