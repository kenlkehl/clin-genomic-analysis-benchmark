"""Localhost Responses pass-through that holds the Azure credential.

OpenCode talks to this proxy with a per-invocation random key. The trusted
adapter process forwards each request to the Azure OpenAI v1 endpoint with a
Microsoft Entra bearer token from ``az account get-access-token``, so the Azure
credential never enters the model-controlled sandbox. Request and response
bodies pass through unchanged except that ``model`` is pinned to the configured
deployment. The audit log records only request settings (model, reasoning
effort, storage), status codes, timing, and token usage.
"""

from __future__ import annotations

import json
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
from typing import Any, Callable, Mapping


AZURE_RESOURCE = "https://cognitiveservices.azure.com/"
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_TAPPED_LINE_BYTES = 64 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
_TERMINAL_EVENTS = {"response.completed", "response.incomplete", "response.failed"}


class BridgeError(RuntimeError):
    """A sanitized proxy error safe to return to the OpenCode client."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BridgeConfig:
    endpoint: str
    model: str
    request_timeout_seconds: float = 1800.0
    max_retries: int = 6
    retry_base_seconds: float = 5.0
    max_retry_sleep_seconds: float = 60.0
    max_requests: int = 512
    token_reuse_seconds: float = 1800.0

    @property
    def responses_url(self) -> str:
        return f"{self.endpoint.rstrip('/')}/responses"


class AzureTokenSource:
    """Fetch and briefly reuse an Entra ID token for Azure OpenAI."""

    def __init__(
        self,
        *,
        reuse_seconds: float = 1800.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.reuse_seconds = reuse_seconds
        self._runner = runner
        self._lock = threading.Lock()
        self._token: str | None = None
        self._fetched_at = 0.0

    def token(self) -> str:
        with self._lock:
            if (
                self._token is not None
                and time.monotonic() - self._fetched_at < self.reuse_seconds
            ):
                return self._token
            proc = self._runner(
                [
                    "az", "account", "get-access-token",
                    f"--resource={AZURE_RESOURCE}",
                    "--query", "accessToken",
                    "--output", "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            token = (proc.stdout or "").strip()
            if proc.returncode != 0 or not token:
                # Do not echo stdout: on partial success it could hold a token.
                raise BridgeError(
                    "az account get-access-token failed: "
                    f"{(proc.stderr or '').strip()[-1000:]}"
                )
            self._token = token
            self._fetched_at = time.monotonic()
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


def request_settings(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content-free request settings worth auditing."""
    reasoning = body.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, Mapping) else {}
    include = body.get("include")
    return {
        "requested_model": body.get("model"),
        "reasoning_effort": reasoning.get("effort"),
        "reasoning_summary": reasoning.get("summary"),
        "store": body.get("store"),
        "include": list(include) if isinstance(include, list) else include,
        "max_output_tokens": body.get("max_output_tokens"),
        "temperature_set": "temperature" in body,
        "stream": bool(body.get("stream")),
        "input_item_count": len(body.get("input") or []),
        "tool_count": len(body.get("tools") or []),
    }


def response_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return status, effective settings, and usage from a Responses object."""
    reasoning = response.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, Mapping) else {}
    incomplete = response.get("incomplete_details")
    error = response.get("error")
    return {
        "response_status": response.get("status"),
        "response_model": response.get("model"),
        "effective_reasoning_effort": reasoning.get("effort"),
        "incomplete_reason": (
            incomplete.get("reason") if isinstance(incomplete, Mapping) else None
        ),
        "error_code": error.get("code") if isinstance(error, Mapping) else None,
        "usage": response.get("usage"),
        "output_item_count": len(response.get("output") or []),
    }


class StreamTap:
    """Watch a forwarded SSE stream for its terminal ``response`` object."""

    def __init__(self) -> None:
        self._pending = b""
        self._overflowed = False
        self.summary: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()
        if len(self._pending) > MAX_TAPPED_LINE_BYTES:
            self._pending = b""
            self._overflowed = True
        for line in lines:
            self._line(line)

    def close(self) -> None:
        if self._pending:
            self._line(self._pending)
            self._pending = b""

    def _line(self, line: bytes) -> None:
        if self._overflowed:
            self._overflowed = False
            return
        line = line.rstrip(b"\r")
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not any(name.encode() in payload for name in _TERMINAL_EVENTS):
            return
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if not isinstance(event, Mapping) or event.get("type") not in _TERMINAL_EVENTS:
            return
        response = event.get("response")
        if isinstance(response, Mapping):
            self.summary = {"terminal_event": event["type"], **response_summary(response)}


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


def _retry_after_seconds(headers: Any) -> float:
    if headers is None:
        return 0.0
    for name, scale in (("retry-after-ms", 0.001), ("Retry-After", 1.0)):
        value = headers.get(name)
        if value:
            try:
                return max(0.0, float(value) * scale)
            except ValueError:
                continue
    return 0.0


class AzureResponsesBridge(AbstractContextManager["AzureResponsesBridge"]):
    """Run a localhost-only Azure Responses proxy on an ephemeral TCP port."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        audit_path: Path,
        token_source: AzureTokenSource | None = None,
    ):
        self.config = config
        self.audit_path = audit_path
        self.token_source = token_source or AzureTokenSource(
            reuse_seconds=config.token_reuse_seconds
        )
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
                raise BridgeError("per-invocation bridge request limit exceeded", status=429)
            return self._request_count

    def open_upstream(self, encoded: bytes, *, request_number: int) -> Any:
        """Open the upstream response, retrying before any byte is forwarded.

        Returns the open HTTP response on success. On a final HTTP failure
        returns the ``HTTPError`` (also a readable response) so its status and
        body can be relayed to OpenCode unchanged.
        """
        refreshed_after_401 = False
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            request = urllib.request.Request(
                self.config.responses_url,
                data=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                    "Authorization": f"Bearer {self.token_source.token()}",
                },
            )
            try:
                response = urllib.request.urlopen(
                    request, timeout=self.config.request_timeout_seconds
                )
                self._audit({
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": response.status,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "request_id": response.headers.get("x-request-id")
                    or response.headers.get("apim-request-id"),
                })
                return response
            except urllib.error.HTTPError as exc:
                # Entra tokens can lapse inside the reuse window. Retry a 401
                # once, at once, with a fresh token; a second 401 is real.
                refresh_token = exc.code == 401 and not refreshed_after_401
                if exc.code == 401:
                    self.token_source.invalidate()
                    refreshed_after_401 = True
                retryable = refresh_token or exc.code in RETRYABLE_STATUS_CODES
                last = attempt >= self.config.max_retries
                record = {
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": exc.code,
                    "retryable": retryable,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                if not retryable or last:
                    body = exc.read(MAX_REQUEST_BYTES)
                    record["error"] = _error_message(body)
                    self._audit(record)
                    return _ReplayedHTTPError(exc, body)
                record["error"] = _error_message(exc.read(8192))
                self._audit(record)
                if refresh_token:
                    continue
                sleep_for = _retry_after_seconds(exc.headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = attempt >= self.config.max_retries
                self._audit({
                    "event": "upstream_attempt",
                    "request_number": request_number,
                    "attempt": attempt,
                    "status": None,
                    "retryable": not last,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc)[:2000],
                })
                if last:
                    raise BridgeError(
                        f"Azure Responses transport failed: {str(exc)[:500]}"
                    ) from exc
                sleep_for = 0.0
            sleep_for = max(
                sleep_for,
                min(
                    self.config.retry_base_seconds * (2 ** (attempt - 1)),
                    self.config.max_retry_sleep_seconds,
                ),
            )
            time.sleep(min(sleep_for, self.config.max_retry_sleep_seconds))

    def __enter__(self) -> "AzureResponsesBridge":
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
                self.close_connection = True

            def _authorized(self) -> bool:
                expected = f"Bearer {bridge.bearer_token}"
                if not secrets.compare_digest(
                    self.headers.get("Authorization", ""), expected
                ):
                    self._json(401, {"error": {"message": "unauthorized bridge request"}})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                self._json(404, {"error": {"message": "not found"}})

            def _relay(self, upstream: Any, request_number: int) -> None:
                status = getattr(upstream, "status", None) or upstream.code
                content_type = upstream.headers.get(
                    "Content-Type", "application/json"
                )
                streaming = "text/event-stream" in content_type
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                tap = StreamTap() if streaming else None
                collected = bytearray()
                forwarded = 0
                try:
                    while True:
                        chunk = upstream.read1(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        forwarded += len(chunk)
                        if tap is not None:
                            tap.feed(chunk)
                        elif len(collected) < MAX_REQUEST_BYTES:
                            collected.extend(chunk)
                        self.wfile.write(f"{len(chunk):x}\r\n".encode())
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                finally:
                    self.close_connection = True
                    upstream.close()
                    summary: dict[str, Any] | None = None
                    if tap is not None:
                        tap.close()
                        summary = tap.summary
                    elif status == 200:
                        try:
                            parsed = json.loads(bytes(collected))
                        except ValueError:
                            parsed = None
                        if isinstance(parsed, Mapping):
                            summary = response_summary(parsed)
                    bridge._audit({
                        "event": "response",
                        "request_number": request_number,
                        "status": status,
                        "forwarded_bytes": forwarded,
                        **(summary or {"terminal_event": None}),
                    })

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
                        raise BridgeError("invalid or oversized request body", status=413)
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise BridgeError("request JSON must be an object", status=400)
                    bridge._audit({
                        "event": "request",
                        "request_number": request_number,
                        **request_settings(body),
                    })
                    body["model"] = bridge.config.model
                    upstream = bridge.open_upstream(
                        json.dumps(body).encode(), request_number=request_number
                    )
                    self._relay(upstream, request_number)
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
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": {"message": f"invalid request: {exc}"}})
                except (BrokenPipeError, ConnectionResetError):
                    bridge._audit({"event": "client_disconnected"})
                except Exception as exc:
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
            name="azure-responses-bridge",
            daemon=True,
        )
        self.thread.start()
        self._audit({
            "event": "bridge_started",
            "endpoint": self.config.endpoint,
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


class _ReplayedHTTPError:
    """An already-read upstream error response, relayed like a live one."""

    def __init__(self, exc: urllib.error.HTTPError, body: bytes):
        self.code = exc.code
        self.status = exc.code
        self.headers = exc.headers
        self._body = body
        self._sent = False

    def read1(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._body

    def close(self) -> None:
        return None
