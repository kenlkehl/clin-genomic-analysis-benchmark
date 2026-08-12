"""Credential-shielding Responses proxy for local Unsloth Studio.

Codex connects to this localhost-only server with a random per-invocation key.
The bridge adds the real Studio API key only to its trusted upstream request,
so model-launched shell commands cannot read the long-lived credential.
"""

from __future__ import annotations

import copy
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from adapters.codex_vertex_gemma4_26b.vertex_bridge import response_events


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_REQUEST_BYTES = 64 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 1_200.0


class BridgeError(RuntimeError):
    """A sanitized proxy error safe to return to the Codex client."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    model: str
    api_key: str
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = 3
    retry_base_seconds: float = 2.0
    max_retry_sleep_seconds: float = 30.0
    max_requests: int = 256
    max_output_tokens: int | None = None

    @property
    def responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"


class UnslothResponsesClient:
    def __init__(
        self,
        config: BridgeConfig,
        audit: Callable[[dict[str, Any]], None],
    ):
        self.config = config
        self.audit = audit

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw.decode(errors="replace")[:2000]
        if isinstance(parsed, Mapping) and isinstance(parsed.get("error"), Mapping):
            parsed = parsed["error"]
        if isinstance(parsed, Mapping):
            return str(parsed.get("message") or parsed)[:2000]
        return str(parsed)[:2000]

    def complete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        upstream = copy.deepcopy(dict(payload))
        upstream["model"] = self.config.model
        if self.config.max_output_tokens is not None:
            requested = upstream.get("max_output_tokens")
            upstream["max_output_tokens"] = (
                min(requested, self.config.max_output_tokens)
                if isinstance(requested, int) and requested > 0
                else self.config.max_output_tokens
            )
        # Buffer one complete response so transport failures can be retried and
        # then replay it to Codex as a standards-compliant SSE sequence.
        upstream["stream"] = False
        encoded = json.dumps(upstream).encode()
        last_error = "Unsloth Studio request failed"
        for attempt in range(1, self.config.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                self.config.responses_url,
                data=encoded,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.request_timeout_seconds
                ) as response:
                    raw = response.read(MAX_REQUEST_BYTES)
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise BridgeError(
                            "Unsloth Studio returned a non-object JSON response"
                        )
                    self.audit({
                        "event": "upstream_attempt",
                        "attempt": attempt,
                        "status": response.status,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "request_id": response.headers.get("x-request-id"),
                    })
                    return parsed
            except urllib.error.HTTPError as exc:
                last_error = self._error_message(exc.read(8192))
                retryable = exc.code in RETRYABLE_STATUS_CODES
                self.audit({
                    "event": "upstream_attempt",
                    "attempt": attempt,
                    "status": exc.code,
                    "retryable": retryable,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": last_error,
                })
                if not retryable or attempt >= self.config.max_retries:
                    raise BridgeError(
                        f"Unsloth Responses failed with HTTP {exc.code}: {last_error}",
                        status=502 if retryable else exc.code,
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                try:
                    sleep_for = float(retry_after) if retry_after else 0.0
                except ValueError:
                    sleep_for = 0.0
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)[:2000]
                self.audit({
                    "event": "upstream_attempt",
                    "attempt": attempt,
                    "status": None,
                    "retryable": True,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": last_error,
                })
                if attempt >= self.config.max_retries:
                    raise BridgeError(
                        f"Unsloth Responses transport failed: {last_error}"
                    ) from exc
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


class UnslothResponsesBridge(AbstractContextManager["UnslothResponsesBridge"]):
    """Run an authenticated localhost proxy on an ephemeral TCP port."""

    def __init__(self, config: BridgeConfig, *, audit_path: Path):
        self.config = config
        self.audit_path = audit_path
        self.bearer_token = secrets.token_urlsafe(32)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._audit_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_count = 0

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
                raise BridgeError(
                    "per-invocation bridge request limit exceeded", status=429
                )
            return self._request_count

    def __enter__(self) -> "UnslothResponsesBridge":
        bridge = self
        client = UnslothResponsesClient(self.config, self._audit)

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
                if not secrets.compare_digest(
                    self.headers.get("Authorization", ""), expected
                ):
                    self._json(
                        401, {"error": {"message": "unauthorized bridge request"}}
                    )
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path.rstrip("/") in {"/v1/models", "/models"}:
                    self._json(200, {
                        "object": "list",
                        "data": [{
                            "id": bridge.config.model,
                            "object": "model",
                            "owned_by": "unsloth",
                        }],
                    })
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path.rstrip("/") not in {"/v1/responses", "/responses"}:
                    self._json(404, {"error": {"message": "not found"}})
                    return
                try:
                    request_number = bridge._next_request()
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > MAX_REQUEST_BYTES:
                        raise BridgeError(
                            "invalid or oversized request body", status=413
                        )
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise BridgeError("request JSON must be an object", status=400)
                    bridge._audit({
                        "event": "request",
                        "request_number": request_number,
                        "input_item_count": len(body.get("input") or []),
                        "tool_count": len(body.get("tools") or []),
                        "stream": bool(body.get("stream")),
                    })
                    response = client.complete(body)
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for event in response_events(response):
                            event_type = event["type"]
                            payload = json.dumps(event, separators=(",", ":"))
                            self.wfile.write(
                                f"event: {event_type}\ndata: {payload}\n\n".encode()
                            )
                        self.wfile.flush()
                        self.close_connection = True
                    else:
                        self._json(200, response)
                    bridge._audit({
                        "event": "response",
                        "request_number": request_number,
                        "output_item_count": len(response.get("output") or []),
                    })
                except BridgeError as exc:
                    bridge._audit({
                        "event": "bridge_error",
                        "status": exc.status,
                        "error": str(exc),
                    })
                    self._json(
                        exc.status,
                        {"error": {"message": str(exc), "type": "bridge_error"}},
                    )
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": {"message": f"invalid request: {exc}"}})
                except (BrokenPipeError, ConnectionResetError):
                    bridge._audit({"event": "client_disconnected"})
                except Exception as exc:
                    bridge._audit({
                        "event": "bridge_error",
                        "status": 500,
                        "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                    })
                    self._json(500, {"error": {"message": "internal bridge error"}})

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="unsloth-responses-bridge",
            daemon=True,
        )
        self.thread.start()
        self._audit({
            "event": "bridge_started",
            "base_url": self.config.base_url,
            "model": self.config.model,
        })
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._audit({"event": "bridge_stopped", "request_count": self._request_count})
