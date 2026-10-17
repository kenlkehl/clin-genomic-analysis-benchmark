"""Local Responses proxy that repairs unparsed Gemma 4 tool-call markup.

Gemma 4 emits a custom ``<|tool_call>`` protocol. A vLLM server normally
converts it into Responses ``function_call`` items when launched with the
Gemma 4 tool parser. This proxy is a compatibility layer for deployments that
serve the markup as ordinary output text instead. Already parsed responses pass
through unchanged.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from adapters.codex_vertex_gemma4_26b.vertex_bridge import response_events


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_REQUEST_BYTES = 64 * 1024 * 1024
TOOL_CALL_START = "<|tool_call>"
TOOL_CALL_END = "<tool_call|>"
STRING_DELIMITER = '<|"|>'
_CALL_RE = re.compile(
    re.escape(TOOL_CALL_START)
    + r"\s*call:([A-Za-z_][A-Za-z0-9_.-]*)\s*"
    + r"(\{[\s\S]*?\})\s*"
    + re.escape(TOOL_CALL_END)
)


class BridgeError(RuntimeError):
    """A sanitized proxy error safe to return to the Codex client."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    model: str
    api_key: str | None = None
    request_timeout_seconds: float = 600.0
    max_retries: int = 3
    retry_base_seconds: float = 2.0
    max_retry_sleep_seconds: float = 30.0
    max_requests: int = 256

    @property
    def responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"


class _ArgumentsParser:
    """Parse Gemma 4's compact, non-JSON tool argument representation."""

    def __init__(self, value: str):
        self.value = value
        self.position = 0

    def parse(self) -> dict[str, Any]:
        parsed = self._object()
        self._space()
        if self.position != len(self.value):
            raise ValueError("unexpected trailing tool arguments")
        return parsed

    def _space(self) -> None:
        while self.position < len(self.value) and self.value[self.position].isspace():
            self.position += 1

    def _take(self, token: str) -> None:
        self._space()
        if not self.value.startswith(token, self.position):
            raise ValueError(f"expected {token!r} in tool arguments")
        self.position += len(token)

    def _object(self) -> dict[str, Any]:
        self._take("{")
        result: dict[str, Any] = {}
        self._space()
        if self.value.startswith("}", self.position):
            self.position += 1
            return result
        while True:
            key = self._key()
            self._take(":")
            result[key] = self._value()
            self._space()
            if self.value.startswith("}", self.position):
                self.position += 1
                return result
            self._take(",")

    def _array(self) -> list[Any]:
        self._take("[")
        result: list[Any] = []
        self._space()
        if self.value.startswith("]", self.position):
            self.position += 1
            return result
        while True:
            result.append(self._value())
            self._space()
            if self.value.startswith("]", self.position):
                self.position += 1
                return result
            self._take(",")

    def _key(self) -> str:
        self._space()
        if self.value.startswith(STRING_DELIMITER, self.position):
            return str(self._delimited_string())
        if self.value.startswith('"', self.position):
            return str(self._json_value())
        start = self.position
        while self.position < len(self.value):
            character = self.value[self.position]
            if character == ":" or character.isspace():
                break
            self.position += 1
        key = self.value[start:self.position]
        if not key:
            raise ValueError("empty tool argument key")
        return key

    def _delimited_string(self) -> str:
        self._take(STRING_DELIMITER)
        end = self.value.find(STRING_DELIMITER, self.position)
        if end < 0:
            raise ValueError("unterminated Gemma string delimiter")
        result = self.value[self.position:end]
        self.position = end + len(STRING_DELIMITER)
        return result

    def _json_value(self) -> Any:
        decoder = json.JSONDecoder()
        parsed, length = decoder.raw_decode(self.value[self.position:])
        self.position += length
        return parsed

    def _value(self) -> Any:
        self._space()
        if self.value.startswith(STRING_DELIMITER, self.position):
            return self._delimited_string()
        if self.value.startswith("{", self.position):
            return self._object()
        if self.value.startswith("[", self.position):
            return self._array()
        if self.value.startswith('"', self.position):
            return self._json_value()
        start = self.position
        while self.position < len(self.value):
            character = self.value[self.position]
            if character in ",]}" or character.isspace():
                break
            self.position += 1
        token = self.value[start:self.position]
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "null":
            return None
        try:
            return float(token) if any(mark in token for mark in ".eE") else int(token)
        except ValueError:
            if not token:
                raise ValueError("missing tool argument value") from None
            return token


def parse_gemma_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for match in _CALL_RE.finditer(text):
        calls.append((match.group(1), _ArgumentsParser(match.group(2)).parse()))
    return calls


def _tool_kinds(tools: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or "").strip()
        if not name and isinstance(tool.get("function"), Mapping):
            name = str(tool["function"].get("name") or "").strip()
        if name:
            result[name] = str(tool.get("type") or "function")
    return result


def _message_text(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for part in item.get("content") or []:
        if isinstance(part, Mapping) and part.get("type") == "output_text":
            parts.append(str(part.get("text") or ""))
    return "\n".join(parts)


def _message_item(text: str, original: Mapping[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(dict(original))
    item["content"] = [{
        "type": "output_text",
        "text": text,
        "annotations": [],
    }]
    return item


def repair_gemma_tool_calls(
    response: Mapping[str, Any], *, tools: Any
) -> tuple[dict[str, Any], int]:
    """Convert raw Gemma tool markup in message text into Responses call items."""
    repaired = copy.deepcopy(dict(response))
    kinds = _tool_kinds(tools)
    output: list[dict[str, Any]] = []
    repaired_count = 0
    for raw_item in repaired.get("output") or []:
        if not isinstance(raw_item, Mapping):
            continue
        if raw_item.get("type") == "reasoning":
            # Codex does not need the model's private chain-of-thought, and the
            # buffered SSE helper intentionally emits only actionable items.
            continue
        if raw_item.get("type") != "message":
            output.append(dict(raw_item))
            continue
        text = _message_text(raw_item)
        calls = parse_gemma_tool_calls(text)
        if not calls:
            output.append(dict(raw_item))
            continue
        clean_text = _CALL_RE.sub("", text).strip()
        if clean_text:
            output.append(_message_item(clean_text, raw_item))
        for name, arguments in calls:
            call_id = f"call_{uuid.uuid4().hex}"
            item_id = f"fc_{uuid.uuid4().hex}"
            if kinds.get(name) == "custom":
                tool_input: Any = arguments.get("input")
                if tool_input is None and len(arguments) == 1:
                    tool_input = next(iter(arguments.values()))
                if tool_input is None:
                    tool_input = json.dumps(arguments, separators=(",", ":"))
                output.append({
                    "type": "custom_tool_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": name,
                    "input": str(tool_input),
                    "status": "completed",
                })
            else:
                output.append({
                    "type": "function_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, separators=(",", ":")),
                    "status": "completed",
                })
            repaired_count += 1
    repaired["output"] = output
    return repaired, repaired_count


class VLLMResponsesClient:
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
        upstream["stream"] = False
        encoded = json.dumps(upstream).encode()
        last_error = "vLLM request failed"
        for attempt in range(1, self.config.max_retries + 1):
            started = time.monotonic()
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            request = urllib.request.Request(
                self.config.responses_url,
                data=encoded,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.request_timeout_seconds
                ) as response:
                    raw = response.read(MAX_REQUEST_BYTES)
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise BridgeError("vLLM returned a non-object JSON response")
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
                        f"vLLM Responses failed with HTTP {exc.code}: {last_error}",
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
                        f"vLLM Responses transport failed: {last_error}"
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


class VLLMResponsesBridge(AbstractContextManager["VLLMResponsesBridge"]):
    """Run a localhost-only compatibility proxy on an ephemeral TCP port."""

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
                raise BridgeError("per-invocation bridge request limit exceeded", status=429)
            return self._request_count

    def __enter__(self) -> "VLLMResponsesBridge":
        bridge = self
        client = VLLMResponsesClient(self.config, self._audit)

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
                    self._json(401, {"error": {"message": "unauthorized bridge request"}})
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
                            "owned_by": "vllm",
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
                        raise BridgeError("invalid or oversized request body", status=413)
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
                    upstream = client.complete(body)
                    response, repaired_count = repair_gemma_tool_calls(
                        upstream, tools=body.get("tools")
                    )
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
                        "repaired_tool_call_count": repaired_count,
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
            name="vllm-gemma-responses-bridge",
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
