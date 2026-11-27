"""Localhost pass-through bridge from Codex to a partner model on Vertex's Responses API.

Codex custom model providers speak the OpenAI Responses API, and Vertex serves
some partner models (for example Meta's Muse Ember) on a per-publisher
Responses endpoint, so no protocol translation is needed. This trusted,
localhost-only bridge exists to keep Google credentials outside the
model-controlled Codex sandbox: Codex authenticates to the bridge with an
ephemeral bearer key, and the bridge forwards each request upstream with a
fresh Google access token, the bare model id, and nothing else changed except
request fields listed in ``UNSUPPORTED_REQUEST_FIELDS``.

Streams are relayed line by line as they arrive. Retries (429, 5xx, transport
errors, and a 401 once with a refreshed token) happen only before any byte has
been sent back to Codex. The audit log records request and response metadata
(counts, types, statuses, token usage), never prompt or output text.
"""

from __future__ import annotations

import copy
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.codex_vertex_gemma4_26b.vertex_bridge import (  # noqa: E402
    MAX_REQUEST_BYTES,
    RETRYABLE_STATUS_CODES,
    BridgeError,
    GoogleAccessTokenSource,
)

DEFAULT_LOCATION = "global"
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
# Request fields the upstream rejects. Each entry is removed before forwarding
# and the removal is recorded in the audit log. Empty until a field is shown
# to fail; Vertex's Ember endpoint accepted every field Codex 0.169 sent in
# the 2026-11-27 smoke test.
UNSUPPORTED_REQUEST_FIELDS: tuple[str, ...] = ()
_TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


@dataclass(frozen=True)
class ResponsesBridgeConfig:
    project_id: str
    model: str  # "<publisher>/<model>", e.g. "meta/muse-ember-1.0"
    location: str = DEFAULT_LOCATION
    reasoning_effort: str | None = None
    request_timeout_seconds: float = 900.0
    max_retries: int = 6
    retry_base_seconds: float = 5.0
    max_retry_sleep_seconds: float = 60.0
    max_requests: int = 256

    def __post_init__(self) -> None:
        publisher, sep, name = self.model.partition("/")
        if not sep or not publisher or not name or "/" in name:
            raise ValueError(
                f"model must be <publisher>/<model>, e.g. meta/muse-ember-1.0; got {self.model!r}"
            )
        if not self.project_id or "/" in self.project_id:
            raise ValueError("project_id must be a Google Cloud project ID")
        if not self.location or "/" in self.location:
            raise ValueError("location must be a location name")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {self.reasoning_effort}")

    @property
    def publisher(self) -> str:
        return self.model.partition("/")[0]

    @property
    def model_id(self) -> str:
        """The bare model id the Responses request names."""
        return self.model.partition("/")[2]

    @property
    def responses_url(self) -> str:
        host = (
            "aiplatform.googleapis.com"
            if self.location == "global"
            else f"{self.location}-aiplatform.googleapis.com"
        )
        return (
            f"https://{host}/v1/projects/{self.project_id}/locations/{self.location}"
            f"/publishers/{self.publisher}/v1/responses"
        )


def prepare_request(
    body: Mapping[str, Any], config: ResponsesBridgeConfig
) -> tuple[dict[str, Any], list[str]]:
    """Return the upstream request and the names of fields removed from it."""
    request = copy.deepcopy(dict(body))
    stripped = [name for name in UNSUPPORTED_REQUEST_FIELDS if name in request]
    for name in stripped:
        request.pop(name)
    request["model"] = config.model_id
    if config.reasoning_effort is not None:
        reasoning = request.get("reasoning")
        reasoning = dict(reasoning) if isinstance(reasoning, Mapping) else {}
        reasoning["effort"] = config.reasoning_effort
        request["reasoning"] = reasoning
    return request, stripped


def request_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    """Content-free description of a Responses request."""
    items = body.get("input")
    item_types: Counter[str] = Counter()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping):
                item_types[str(item.get("type") or item.get("role") or "unknown")] += 1
    elif isinstance(items, str):
        item_types["text"] += 1
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), Mapping) else {}
    return {
        "stream": bool(body.get("stream")),
        "input_item_types": dict(sorted(item_types.items())),
        "tool_types": dict(sorted(Counter(
            str(tool.get("type")) for tool in tools if isinstance(tool, Mapping)
        ).items())),
        "reasoning_effort": reasoning.get("effort"),
        "fields": sorted(body),
    }


def response_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    """Content-free description of a final Responses object."""
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    output = response.get("output") if isinstance(response.get("output"), list) else []
    return {
        "response_status": response.get("status"),
        "output_item_types": dict(sorted(Counter(
            str(item.get("type")) for item in output if isinstance(item, Mapping)
        ).items())),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": in_details.get("cached_tokens") if isinstance(in_details, Mapping) else None,
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": out_details.get("reasoning_tokens") if isinstance(out_details, Mapping) else None,
        "incomplete_reason": (response.get("incomplete_details") or {}).get("reason")
        if isinstance(response.get("incomplete_details"), Mapping) else None,
    }


def _error_message(raw: bytes) -> str:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode(errors="replace")[:1000]
    candidate: Any = parsed
    if isinstance(candidate, list) and candidate:
        candidate = candidate[0]
    if isinstance(candidate, Mapping) and isinstance(candidate.get("error"), Mapping):
        candidate = candidate["error"]
    if isinstance(candidate, Mapping):
        return str(candidate.get("message") or candidate)[:1000]
    return str(candidate)[:1000]


class VertexResponsesClient:
    """Open an upstream Responses request, retrying before any byte is relayed."""

    def __init__(
        self,
        config: ResponsesBridgeConfig,
        token_source: GoogleAccessTokenSource,
        audit: Callable[[dict[str, Any]], None],
    ):
        self.config = config
        self.token_source = token_source
        self.audit = audit

    def open(self, payload: Mapping[str, Any], *, request_number: int) -> Any:
        """Return an open HTTP response (status 2xx) or raise BridgeError."""
        encoded = json.dumps(payload).encode()
        last_error = "Vertex Responses request failed"
        refreshed_after_401 = False
        for attempt in range(1, self.config.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                self.config.responses_url,
                data=encoded,
                headers={
                    "Authorization": f"Bearer {self.token_source.get()}",
                    "Content-Type": "application/json",
                    "X-Goog-User-Project": self.config.project_id,
                },
            )
            try:
                response = urllib.request.urlopen(
                    request, timeout=self.config.request_timeout_seconds
                )
                self.audit({
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": response.status,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                })
                return response
            except urllib.error.HTTPError as exc:
                raw = exc.read(8192)
                last_error = _error_message(raw)
                refresh_token = exc.code == 401 and not refreshed_after_401
                if exc.code == 401:
                    self.token_source.invalidate()
                    refreshed_after_401 = True
                retryable = refresh_token or exc.code in RETRYABLE_STATUS_CODES
                self.audit({
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": exc.code,
                    "retryable": retryable,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": last_error,
                })
                if not retryable or attempt >= self.config.max_retries:
                    raise BridgeError(
                        f"Vertex Responses failed with HTTP {exc.code}: {last_error}",
                        status=502 if retryable else exc.code,
                    ) from exc
                if refresh_token:
                    continue
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    sleep_for = float(retry_after) if retry_after else 0.0
                except ValueError:
                    sleep_for = 0.0
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = str(exc)[:1000]
                self.audit({
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": None,
                    "retryable": True,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": last_error,
                })
                if attempt >= self.config.max_retries:
                    raise BridgeError(f"Vertex Responses transport failed: {last_error}") from exc
                sleep_for = 0.0
            sleep_for = max(
                sleep_for,
                min(
                    self.config.retry_base_seconds * (2 ** (attempt - 1)),
                    self.config.max_retry_sleep_seconds,
                ),
            )
            time.sleep(sleep_for)
        raise BridgeError(last_error)


class VertexResponsesPassThroughBridge(AbstractContextManager["VertexResponsesPassThroughBridge"]):
    """Serve the pass-through on an ephemeral localhost TCP port."""

    def __init__(
        self,
        config: ResponsesBridgeConfig,
        *,
        audit_path: Path,
        token_source: GoogleAccessTokenSource | None = None,
    ):
        self.config = config
        self.audit_path = audit_path
        self.bearer_token = secrets.token_urlsafe(32)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._audit_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_count = 0
        self._token_source = token_source or GoogleAccessTokenSource()

    @property
    def base_url(self) -> str:
        if self.server is None:
            raise RuntimeError("bridge has not started")
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def _audit(self, record: dict[str, Any]) -> None:
        safe = {"timestamp": time.time(), **record}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_lock:
            with self.audit_path.open("a") as handle:
                handle.write(json.dumps(safe, sort_keys=True) + "\n")

    def _next_request(self) -> int:
        with self._request_lock:
            self._request_count += 1
            if self._request_count > self.config.max_requests:
                raise BridgeError("per-invocation bridge request limit exceeded", status=429)
            return self._request_count

    def __enter__(self) -> "VertexResponsesPassThroughBridge":
        bridge = self
        client = VertexResponsesClient(self.config, self._token_source, self._audit)

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

            def _authorized(self) -> bool:
                expected = f"Bearer {bridge.bearer_token}"
                if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
                    self._json(401, {"error": {"message": "unauthorized localhost bridge request"}})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path.rstrip("/") in {"/v1/models", "/models"}:
                    self._json(200, {
                        "object": "list",
                        "data": [{
                            "id": bridge.config.model_id,
                            "object": "model",
                            "owned_by": bridge.config.publisher,
                        }],
                    })
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def _relay_stream(self, upstream: Any, request_number: int) -> None:
                self.send_response(upstream.status)
                self.send_header(
                    "Content-Type", upstream.headers.get("Content-Type", "text/event-stream")
                )
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                final: Mapping[str, Any] | None = None
                terminal_event = None
                event_count = 0
                for line in upstream:
                    self.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        self.wfile.flush()
                    if not line.startswith(b"data:"):
                        continue
                    event_count += 1
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(event, Mapping) and event.get("type") in _TERMINAL_EVENTS:
                        terminal_event = event.get("type")
                        if isinstance(event.get("response"), Mapping):
                            final = event["response"]
                self.wfile.flush()
                self.close_connection = True
                bridge._audit({
                    "event": "response",
                    "request_number": request_number,
                    "stream": True,
                    "sse_data_events": event_count,
                    "terminal_event": terminal_event,
                    **(response_summary(final) if final is not None else {}),
                })

            def _relay_json(self, upstream: Any, request_number: int) -> None:
                raw = upstream.read(MAX_REQUEST_BYTES)
                self.send_response(upstream.status)
                self.send_header(
                    "Content-Type", upstream.headers.get("Content-Type", "application/json")
                )
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                bridge._audit({
                    "event": "response",
                    "request_number": request_number,
                    "stream": False,
                    **(response_summary(parsed) if isinstance(parsed, Mapping) else {}),
                })

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path.rstrip("/") not in {"/v1/responses", "/responses"}:
                    bridge._audit({"event": "unsupported_path", "path": self.path[:200]})
                    self._json(404, {"error": {"message": "not found"}})
                    return
                try:
                    request_number = bridge._next_request()
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > MAX_REQUEST_BYTES:
                        raise BridgeError("invalid or oversized request body", status=413)
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise BridgeError("request JSON must be an object", status=400)
                    upstream_request, stripped = prepare_request(body, bridge.config)
                    bridge._audit({
                        "event": "request",
                        "request_number": request_number,
                        "stripped_fields": stripped,
                        **request_summary(upstream_request),
                    })
                    upstream = client.open(upstream_request, request_number=request_number)
                    with upstream:
                        if upstream_request.get("stream"):
                            self._relay_stream(upstream, request_number)
                        else:
                            self._relay_json(upstream, request_number)
                except BridgeError as exc:
                    bridge._audit({"event": "bridge_error", "status": exc.status, "error": str(exc)})
                    self._json(exc.status, {"error": {"message": str(exc), "type": "bridge_error"}})
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": {"message": f"invalid request: {exc}"}})
                except (BrokenPipeError, ConnectionResetError):
                    bridge._audit({"event": "client_disconnected"})
                except Exception as exc:  # fail closed without a server traceback leak
                    bridge._audit({
                        "event": "bridge_error",
                        "status": 500,
                        "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                    })
                    try:
                        self._json(500, {"error": {"message": "internal bridge error"}})
                    except OSError:
                        pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="vertex-responses-pass-through-bridge",
            daemon=True,
        )
        self.thread.start()
        self._audit({
            "event": "bridge_started",
            "model": self.config.model,
            "location": self.config.location,
            "project_id": self.config.project_id,
            "reasoning_effort": self.config.reasoning_effort,
        })
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._audit({"event": "bridge_stopped", "request_count": self._request_count})
