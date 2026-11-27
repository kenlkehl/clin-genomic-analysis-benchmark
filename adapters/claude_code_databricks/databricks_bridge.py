"""Localhost pass-through from Claude Code to Claude on the Databricks AI Gateway.

The gateway serves the Anthropic Messages API at
``{host}/ai-gateway/anthropic/v1/messages`` with a short-lived Databricks
OAuth token. This trusted bridge runs outside the model-controlled sandbox,
gets its token from ``databricks auth token`` (reused until five minutes
before it expires), and gives Claude Code only an ephemeral bearer key for ``127.0.0.1``. Request and
response bodies pass through unchanged, except that:

- the ``model`` field is rewritten to the gateway endpoint name (Claude Code
  is told the plain Claude ID, so its model-specific behaviour is right);
- features the gateway rejects under its HIPAA configuration are removed
  (``context_management`` and its beta header), and each removal is audited.

The audit file records request metadata and token usage, never content.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 64 * 1024 * 1024
DEFAULT_PROFILE = "unity-gateway"
ANTHROPIC_VERSION = "2023-06-01"
# Body fields and beta-header prefixes the gateway refuses for HIPAA orgs.
STRIPPED_BODY_FIELDS = ("context_management",)
STRIPPED_BETA_PREFIXES = ("context-management-",)
_HOP_HEADERS = {"connection", "transfer-encoding", "content-length", "content-encoding"}

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
}


class BridgeError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BridgeConfig:
    endpoint: str                    # full gateway endpoint name
    model: str                       # Claude ID that Claude Code is told
    request_timeout_seconds: float = 900.0
    max_requests: int = 500


class DatabricksTokenSource:
    """Bearer tokens from the Databricks CLI login (``databricks auth token``).

    The CLI refreshes the OAuth token from its cached login; the bridge reuses a
    token until five minutes before it expires. The host comes from
    ``DATABRICKS_HOST`` or the profile's entry in ``~/.databrickscfg``.
    Only the standard library is used: the harness runs adapters with the
    system Python, not the project environment.
    """

    REFRESH_MARGIN_SECONDS = 300

    def __init__(self, profile: str | None = None,
                 runner: Callable[[list[str]], str] | None = None,
                 host: str | None = None):
        self.profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE") or DEFAULT_PROFILE
        self._run = runner or self._run_cli
        self._host = host
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _run_cli(command: list[str]) -> str:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            # The CLI's stderr names the problem (e.g. an expired login), not the token.
            raise BridgeError("databricks auth token failed: "
                              + (proc.stderr or "").strip()[:300], status=401)
        return proc.stdout

    @property
    def host(self) -> str:
        if self._host is None:
            host = os.environ.get("DATABRICKS_HOST", "").strip()
            if not host:
                config = configparser.ConfigParser()
                config.read(Path.home() / ".databrickscfg")
                host = config.get(self.profile, "host", fallback="").strip()
            if not host:
                raise BridgeError(f"no Databricks host for profile {self.profile!r}", status=500)
            self._host = host.rstrip("/")
        return self._host

    def headers(self) -> dict[str, str]:
        with self._lock:
            if self._token is None or time.time() > self._expires_at - self.REFRESH_MARGIN_SECONDS:
                raw = self._run(["databricks", "auth", "token", "--profile", self.profile])
                try:
                    reply = json.loads(raw)
                    token = str(reply["access_token"])
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise BridgeError("databricks auth token returned no access token",
                                      status=401) from exc
                expires_in = reply.get("expires_in")
                self._token = token
                self._expires_at = time.time() + (
                    float(expires_in) if isinstance(expires_in, (int, float)) else 600.0)
            return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


def canonical_model(endpoint: str) -> str | None:
    """The Claude model ID at the end of a gateway endpoint name."""
    match = re.search(r"(claude-[a-z]+-\d+(?:-\d+)?)$", endpoint.strip())
    return match.group(1) if match else None


def _filtered_betas(value: str | None) -> tuple[str | None, list[str]]:
    if not value:
        return None, []
    kept, dropped = [], []
    for beta in (part.strip() for part in value.split(",")):
        if not beta:
            continue
        (dropped if beta.startswith(STRIPPED_BETA_PREFIXES) else kept).append(beta)
    return (",".join(kept) or None), dropped


def prepare_request(body: dict[str, Any], headers: Any, config: BridgeConfig
                    ) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    """Return (upstream body, upstream headers without auth, audit facts)."""
    body = dict(body)
    facts: dict[str, Any] = {"requested_model": body.get("model"),
                             "stream": bool(body.get("stream"))}
    if body.get("model") != config.model:
        facts["model_mismatch"] = True
    body["model"] = config.endpoint
    stripped = [name for name in STRIPPED_BODY_FIELDS if name in body]
    for name in stripped:
        body.pop(name)
    out = {"Content-Type": "application/json",
           "anthropic-version": headers.get("anthropic-version") or ANTHROPIC_VERSION,
           "Accept": headers.get("Accept") or "application/json"}
    betas, dropped = _filtered_betas(headers.get("anthropic-beta"))
    if betas:
        out["anthropic-beta"] = betas
    if stripped or dropped:
        facts["stripped"] = {"body_fields": stripped, "betas": dropped}
    effort = (body.get("output_config") or {}).get("effort") if isinstance(
        body.get("output_config"), dict) else None
    if effort:
        facts["effort"] = effort
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        facts["thinking"] = thinking.get("type")
    facts["message_count"] = len(body.get("messages") or [])
    facts["tool_count"] = len(body.get("tools") or [])
    return json.dumps(body).encode(), out, facts


class _UsageScanner:
    """Pick token usage out of SSE ``message_start`` / ``message_delta`` lines."""

    def __init__(self) -> None:
        self.usage: dict[str, int] = {}
        self.stop_reason: str | None = None

    def _merge(self, usage: Any) -> None:
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, int):
                    self.usage[key] = value

    def line(self, raw: bytes) -> None:
        if not raw.startswith(b"data:"):
            return
        try:
            event = json.loads(raw[5:].strip() or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start":
            self._merge((event.get("message") or {}).get("usage"))
        elif event.get("type") == "message_delta":
            self._merge(event.get("usage"))
            self.stop_reason = (event.get("delta") or {}).get("stop_reason") or self.stop_reason

    def message(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(message, dict):
            self._merge(message.get("usage"))
            self.stop_reason = message.get("stop_reason")


class DatabricksAnthropicBridge(AbstractContextManager["DatabricksAnthropicBridge"]):
    """Run the pass-through on an ephemeral localhost TCP port."""

    def __init__(self, config: BridgeConfig, *, audit_path: Path,
                 token_source: DatabricksTokenSource | None = None,
                 upstream_base: str | None = None,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        self.config = config
        self.audit_path = audit_path
        self.bearer_token = secrets.token_urlsafe(32)
        self.tokens = token_source or DatabricksTokenSource()
        self._upstream_base = upstream_base
        self._open = opener
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._audit_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_count = 0
        self.totals: dict[str, int] = {}

    @property
    def base_url(self) -> str:
        """Root for ANTHROPIC_BASE_URL; Claude Code appends ``/v1/messages``."""
        if self.server is None:
            raise RuntimeError("bridge has not started")
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def upstream_url(self) -> str:
        base = self._upstream_base or f"{self.tokens.host}/ai-gateway/anthropic"
        return f"{base.rstrip('/')}/v1/messages"

    def _audit(self, record: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_lock:
            with self.audit_path.open("a") as handle:
                handle.write(json.dumps({"timestamp": time.time(), **record},
                                        sort_keys=True) + "\n")

    def _next_request(self) -> int:
        with self._request_lock:
            self._request_count += 1
            if self._request_count > self.config.max_requests:
                # Not 429: Claude Code would keep retrying a rate limit.
                raise BridgeError("per-invocation bridge request limit exceeded", status=400)
            return self._request_count

    def _add_usage(self, usage: dict[str, int]) -> None:
        with self._request_lock:
            for key, value in usage.items():
                self.totals[key] = self.totals.get(key, 0) + value

    def _upstream(self, data: bytes, headers: dict[str, str]) -> Any:
        """Open the upstream call; retry a 401 once with a fresh client."""
        for attempt in (1, 2):
            request = urllib.request.Request(
                self.upstream_url, data=data, method="POST",
                headers={**headers, **self.tokens.headers()})
            try:
                return self._open(request, timeout=self.config.request_timeout_seconds)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    self.tokens.invalidate()
                    continue
                return exc

    def __enter__(self) -> "DatabricksAnthropicBridge":
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, value: Any) -> None:
                encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)

            def _error(self, status: int, message: str) -> None:
                kind = _ERROR_TYPES.get(status, "api_error")
                self._json(status, {"type": "error", "error": {"type": kind, "message": message}})

            def _authorized(self) -> bool:
                supplied = self.headers.get("x-api-key") or ""
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    supplied = authorization[len("Bearer "):]
                if not secrets.compare_digest(supplied, bridge.bearer_token):
                    self._error(401, "unauthorized localhost bridge request")
                    return False
                return True

            def _path(self) -> str:
                return urlsplit(self.path).path.rstrip("/")

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise BridgeError("invalid or oversized request body", status=413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise BridgeError("request JSON must be an object", status=400)
                return body

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self._path() == "/v1/models":
                    model = bridge.config.model
                    self._json(200, {"data": [{"type": "model", "id": model,
                                               "display_name": model,
                                               "created_at": "1970-01-01T00:00:00Z"}],
                                     "has_more": False, "first_id": model, "last_id": model})
                else:
                    self._error(404, "not found")

            def _relay(self, upstream: Any, number: int, facts: dict[str, Any]) -> None:
                status = getattr(upstream, "status", None) or upstream.getcode()
                scanner = _UsageScanner()
                content_type = upstream.headers.get("Content-Type", "application/json")
                self.send_response(status)
                for name, value in upstream.headers.items():
                    if name.lower() not in _HOP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if "text/event-stream" in content_type:
                    while True:
                        line = upstream.readline()
                        if not line:
                            break
                        scanner.line(line)
                        self.wfile.write(line)
                        if line in (b"\n", b"\r\n"):
                            self.wfile.flush()
                    error_text = None
                else:
                    raw = upstream.read()
                    scanner.message(raw)
                    self.wfile.write(raw)
                    error_text = raw[:500].decode("utf-8", "replace") if status >= 400 else None
                self.wfile.flush()
                self.close_connection = True
                bridge._add_usage(scanner.usage)
                record = {"event": "response", "request_number": number, "status": status,
                          "stop_reason": scanner.stop_reason, "usage": scanner.usage}
                if error_text:
                    record["error"] = error_text
                bridge._audit(record)

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = self._path()
                try:
                    if path == "/v1/messages/count_tokens":
                        # The gateway has no counting route; a rough estimate.
                        self._json(200, {"input_tokens": len(json.dumps(self._body())) // 4})
                        return
                    if path != "/v1/messages":
                        self._error(404, "not found")
                        return
                    number = bridge._next_request()
                    data, headers, facts = prepare_request(self._body(), self.headers,
                                                           bridge.config)
                    bridge._audit({"event": "request", "request_number": number, **facts})
                    upstream = bridge._upstream(data, headers)
                    try:
                        self._relay(upstream, number, facts)
                    finally:
                        upstream.close()
                except BridgeError as exc:
                    bridge._audit({"event": "bridge_error", "status": exc.status, "error": str(exc)})
                    self._error(exc.status, str(exc))
                except (ValueError, TypeError) as exc:
                    self._error(400, f"invalid request: {exc}")
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        bridge._audit({"event": "client_disconnected"})
                        return
                    bridge._audit({"event": "upstream_unreachable",
                                   "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                    try:
                        self._error(502, "Databricks gateway unreachable")
                    except OSError:
                        pass
                except Exception as exc:  # fail closed without leaking a traceback
                    bridge._audit({"event": "bridge_error", "status": 500,
                                   "error": f"{type(exc).__name__}: {str(exc)[:1000]}"})
                    self._error(500, "internal bridge error")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="databricks-anthropic-bridge", daemon=True)
        self.thread.start()
        self._audit({"event": "bridge_started", "model": self.config.model,
                     "endpoint": self.config.endpoint, "profile": self.tokens.profile})
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._audit({"event": "bridge_stopped", "request_count": self._request_count,
                     "usage_totals": self.totals})
