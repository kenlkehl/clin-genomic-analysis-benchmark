"""Narrow Responses-to-Chat bridge for Gemma 4 on Vertex Agent Platform.

Codex custom model providers speak the OpenAI Responses API.  Vertex Agent
Platform currently exposes Gemma 4 through its OpenAI-compatible Chat
Completions endpoint instead.  This trusted, localhost-only bridge translates
between those two protocols and keeps Google credentials outside the
model-controlled Codex sandbox.
"""

from __future__ import annotations

import copy
import json
import secrets
import subprocess
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


DEFAULT_MODEL = "google/gemma-4-26b-a4b-it-maas"
DEFAULT_LOCATION = "global"
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_REQUEST_BYTES = 64 * 1024 * 1024


class BridgeError(RuntimeError):
    """A sanitized bridge error safe to return to the Codex client."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BridgeConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    request_timeout_seconds: float = 600.0
    max_retries: int = 6
    retry_base_seconds: float = 5.0
    max_retry_sleep_seconds: float = 60.0
    max_output_tokens: int = 16_384
    max_requests: int = 256

    @property
    def chat_url(self) -> str:
        return (
            "https://aiplatform.googleapis.com/v1/projects/"
            f"{self.project_id}/locations/{self.location}/endpoints/"
            "openapi/chat/completions"
        )


class GoogleAccessTokenSource:
    """Fetch short-lived Google access tokens without exposing ADC to Codex."""

    def __init__(self, command: tuple[str, ...] = ("gcloud", "auth", "print-access-token")):
        self._command = command
        self._token: str | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._fetched_at = 0.0

    def get(self) -> str:
        with self._lock:
            # gcloud user access tokens normally live for an hour. Refresh well
            # before expiry, and also refresh immediately after an upstream 401.
            if self._token and time.monotonic() - self._fetched_at < 45 * 60:
                return self._token
            proc = subprocess.run(
                self._command,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                # Do not relay gcloud stderr into the model-visible HTTP error;
                # it can contain host account names and configuration paths.
                raise BridgeError(
                    "Google access-token refresh failed; authenticate gcloud "
                    "in the trusted host environment"
                )
            self._token = proc.stdout.strip()
            self._fetched_at = time.monotonic()
            return self._token


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping):
            value = part.get("text")
            if value is not None:
                parts.append(str(value))
    return "\n".join(part for part in parts if part)


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, separators=(",", ":"))


def _responses_tools_to_chat(
    tools: Any,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chat_tools: list[dict[str, Any]] = []
    tool_kinds: dict[str, str] = {}
    if not isinstance(tools, list):
        return chat_tools, tool_kinds
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        kind = str(tool.get("type") or "function")
        name = str(tool.get("name") or "").strip()
        if kind == "function" and isinstance(tool.get("function"), Mapping):
            function = dict(tool["function"])
            name = str(function.get("name") or name).strip()
        else:
            function = {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("parameters") or {
                    "type": "object",
                    "properties": {},
                },
            }
        if not name:
            continue
        if kind == "custom":
            # Chat Completions has no custom/raw-input tool. Present it to Gemma
            # as a one-string function, then restore the custom-tool call shape
            # when translating the answer back to Responses.
            function = {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "The complete raw input for this tool.",
                        }
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                },
            }
        tool_kinds[name] = kind
        chat_tools.append({"type": "function", "function": function})
    return chat_tools, tool_kinds


def _custom_arguments(raw_input: Any) -> str:
    if not isinstance(raw_input, str):
        return json.dumps({"input": raw_input}, separators=(",", ":"))
    return json.dumps({"input": raw_input}, separators=(",", ":"))


def _responses_input_to_chat(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _text_content(instructions)})

    value = body.get("input", [])
    if isinstance(value, str):
        value = [{"role": "user", "content": value}]
    if not isinstance(value, list):
        raise BridgeError("Responses input must be a string or an array", status=400)

    for item in value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "message")
        if kind == "message":
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": _text_content(item.get("content"))})
        elif kind in {"function_call", "custom_tool_call"}:
            name = str(item.get("name") or "")
            call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}")
            raw = item.get("input") if kind == "custom_tool_call" else item.get("arguments")
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": (
                            _custom_arguments(raw)
                            if kind == "custom_tool_call"
                            else _arguments(raw)
                        ),
                    },
                }],
            })
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": _text_content(item.get("output")),
            })
        # Reasoning and bookkeeping items are intentionally not sent to a
        # model whose model card says thinking is unsupported.
    return messages


def responses_request_to_chat(
    body: Mapping[str, Any], config: BridgeConfig
) -> tuple[dict[str, Any], dict[str, str]]:
    tools, tool_kinds = _responses_tools_to_chat(body.get("tools"))
    max_tokens = body.get("max_output_tokens")
    try:
        requested_tokens = int(max_tokens) if max_tokens is not None else config.max_output_tokens
    except (TypeError, ValueError):
        requested_tokens = config.max_output_tokens
    request: dict[str, Any] = {
        "model": config.model,
        "messages": _responses_input_to_chat(body),
        "max_tokens": min(max(1, requested_tokens), config.max_output_tokens),
    }
    if tools:
        request["tools"] = tools
        request["tool_choice"] = body.get("tool_choice", "auto")
        request["parallel_tool_calls"] = bool(body.get("parallel_tool_calls", True))
    if body.get("temperature") is not None:
        request["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        request["top_p"] = body["top_p"]
    return request, tool_kinds


def _custom_input(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return str(arguments)
    if isinstance(parsed, Mapping) and "input" in parsed:
        return str(parsed["input"])
    return arguments


def chat_response_to_response(
    chat: Mapping[str, Any], *, model: str, tool_kinds: Mapping[str, str]
) -> dict[str, Any]:
    choices = chat.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise BridgeError("Vertex returned no Chat Completions choice")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise BridgeError("Vertex returned a malformed Chat Completions message")

    output: list[dict[str, Any]] = []
    content = _text_content(message.get("content"))
    if content:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
        })

    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = str(function.get("name") or "")
            arguments = _arguments(function.get("arguments"))
            call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
            item_id = f"fc_{uuid.uuid4().hex}"
            if tool_kinds.get(name) == "custom":
                output.append({
                    "type": "custom_tool_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": name,
                    "input": _custom_input(arguments),
                    "status": "completed",
                })
            else:
                output.append({
                    "type": "function_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "status": "completed",
                })

    chat_usage = chat.get("usage") if isinstance(chat.get("usage"), Mapping) else {}
    input_tokens = int(chat_usage.get("prompt_tokens") or 0)
    output_tokens = int(chat_usage.get("completion_tokens") or 0)
    response_id = str(chat.get("id") or f"resp_{uuid.uuid4().hex}")
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "temperature": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": int(chat_usage.get("total_tokens") or input_tokens + output_tokens),
        },
    }


def response_events(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Create a complete Responses SSE event sequence from a buffered answer."""
    events: list[dict[str, Any]] = []
    sequence = 0

    def add(event_type: str, **values: Any) -> None:
        nonlocal sequence
        events.append({"type": event_type, "sequence_number": sequence, **values})
        sequence += 1

    started = copy.deepcopy(dict(response))
    started["status"] = "in_progress"
    started["output"] = []
    started["usage"] = None
    add("response.created", response=started)
    add("response.in_progress", response=started)

    for output_index, item_value in enumerate(response.get("output") or []):
        item = dict(item_value)
        added_item = copy.deepcopy(item)
        added_item["status"] = "in_progress"
        kind = item.get("type")
        if kind == "message":
            added_item["content"] = []
        elif kind == "function_call":
            added_item["arguments"] = ""
        elif kind == "custom_tool_call":
            added_item["input"] = ""
        add("response.output_item.added", output_index=output_index, item=added_item)

        if kind == "message":
            for content_index, part_value in enumerate(item.get("content") or []):
                part = dict(part_value)
                empty_part = {**part, "text": ""}
                add(
                    "response.content_part.added",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=content_index,
                    part=empty_part,
                )
                text = str(part.get("text") or "")
                if text:
                    add(
                        "response.output_text.delta",
                        item_id=item["id"],
                        output_index=output_index,
                        content_index=content_index,
                        delta=text,
                        logprobs=[],
                    )
                add(
                    "response.output_text.done",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=content_index,
                    text=text,
                    logprobs=[],
                )
                add(
                    "response.content_part.done",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=content_index,
                    part=part,
                )
        elif kind == "function_call":
            arguments = str(item.get("arguments") or "")
            if arguments:
                add(
                    "response.function_call_arguments.delta",
                    item_id=item["id"],
                    output_index=output_index,
                    delta=arguments,
                )
            add(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=output_index,
                arguments=arguments,
            )
        elif kind == "custom_tool_call":
            tool_input = str(item.get("input") or "")
            if tool_input:
                add(
                    "response.custom_tool_call_input.delta",
                    item_id=item["id"],
                    output_index=output_index,
                    delta=tool_input,
                )
            add(
                "response.custom_tool_call_input.done",
                item_id=item["id"],
                output_index=output_index,
                input=tool_input,
            )
        add("response.output_item.done", output_index=output_index, item=item)

    add("response.completed", response=dict(response))
    return events


class VertexChatClient:
    def __init__(
        self,
        config: BridgeConfig,
        token_source: GoogleAccessTokenSource,
        audit: Callable[[dict[str, Any]], None],
    ):
        self.config = config
        self.token_source = token_source
        self.audit = audit

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw.decode(errors="replace")[:2000]
        candidate: Any = parsed
        if isinstance(candidate, list) and candidate:
            candidate = candidate[0]
        if isinstance(candidate, Mapping) and isinstance(candidate.get("error"), Mapping):
            candidate = candidate["error"]
        if isinstance(candidate, Mapping):
            return str(candidate.get("message") or candidate)[:2000]
        return str(candidate)[:2000]

    def complete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload).encode()
        last_error = "Vertex request failed"
        for attempt in range(1, self.config.max_retries + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                self.config.chat_url,
                data=encoded,
                headers={
                    "Authorization": f"Bearer {self.token_source.get()}",
                    "Content-Type": "application/json",
                    "X-Goog-User-Project": self.config.project_id,
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.request_timeout_seconds
                ) as response:
                    raw = response.read(MAX_REQUEST_BYTES)
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise BridgeError("Vertex returned a non-object JSON response")
                    self.audit({
                        "event": "upstream_attempt",
                        "attempt": attempt,
                        "status": response.status,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "request_id": response.headers.get("x-request-id"),
                    })
                    return parsed
            except urllib.error.HTTPError as exc:
                raw = exc.read(8192)
                last_error = self._error_message(raw)
                if exc.code == 401:
                    self.token_source.invalidate()
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
                        f"Vertex Chat Completions failed with HTTP {exc.code}: {last_error}",
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
                    raise BridgeError(f"Vertex Chat Completions transport failed: {last_error}") from exc
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


class VertexResponsesBridge(AbstractContextManager["VertexResponsesBridge"]):
    """Run the translation service on an ephemeral localhost TCP port."""

    def __init__(self, config: BridgeConfig, *, audit_path: Path):
        self.config = config
        self.audit_path = audit_path
        self.bearer_token = secrets.token_urlsafe(32)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._audit_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_count = 0
        self._token_source = GoogleAccessTokenSource()

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

    def __enter__(self) -> "VertexResponsesBridge":
        bridge = self
        client = VertexChatClient(self.config, self._token_source, self._audit)

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
                            "id": bridge.config.model,
                            "object": "model",
                            "owned_by": "google",
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
                    chat_request, tool_kinds = responses_request_to_chat(body, bridge.config)
                    bridge._audit({
                        "event": "request",
                        "request_number": request_number,
                        "message_count": len(chat_request["messages"]),
                        "tool_count": len(chat_request.get("tools") or []),
                        "stream": bool(body.get("stream")),
                    })
                    chat_response = client.complete(chat_request)
                    response = chat_response_to_response(
                        chat_response,
                        model=bridge.config.model,
                        tool_kinds=tool_kinds,
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
                        "output_item_count": len(response["output"]),
                        "input_tokens": response["usage"]["input_tokens"],
                        "output_tokens": response["usage"]["output_tokens"],
                    })
                except BridgeError as exc:
                    bridge._audit({"event": "bridge_error", "status": exc.status, "error": str(exc)})
                    self._json(exc.status, {"error": {"message": str(exc), "type": "bridge_error"}})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": {"message": f"invalid request: {exc}"}})
                except (BrokenPipeError, ConnectionResetError):
                    bridge._audit({"event": "client_disconnected"})
                except Exception as exc:  # fail closed without a server traceback leak
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
            name="vertex-gemma-responses-bridge",
            daemon=True,
        )
        self.thread.start()
        self._audit({
            "event": "bridge_started",
            "model": self.config.model,
            "location": self.config.location,
            "project_id": self.config.project_id,
        })
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._audit({"event": "bridge_stopped", "request_count": self._request_count})
