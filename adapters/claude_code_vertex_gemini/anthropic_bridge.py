"""Narrow Anthropic-Messages-to-Chat bridge for Gemini on Vertex Agent Platform.

Claude Code speaks the Anthropic Messages API. Vertex Agent Platform serves
Gemini through its OpenAI-compatible Chat Completions endpoint. This trusted,
localhost-only bridge translates between the two and keeps Google credentials
outside the model-controlled Claude Code sandbox. Upstream calls, token refresh,
and retries reuse the Codex/Vertex Gemma bridge.

Gemini 3 rejects a replayed function call whose thought signature is missing,
and Anthropic tool_use blocks have no field for one. The bridge therefore keeps
each call's signature, keyed by the tool_use id it hands Claude Code, and
reattaches it when Claude Code sends the call back.
"""

from __future__ import annotations

import copy
import json
import secrets
import sys
import threading
import time
import uuid
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.codex_vertex_gemma4_26b.vertex_bridge import (  # noqa: E402
    MAX_REQUEST_BYTES,
    BridgeConfig,
    BridgeError,
    GoogleAccessTokenSource,
    VertexChatClient,
)

REASONING_EFFORTS = ("minimal", "low", "medium", "high")
# Google's documented placeholder for a function call that has no recorded
# signature. Used only when Claude Code replays a call the bridge never issued.
SIGNATURE_PLACEHOLDER = "skip_thought_signature_validator"

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
}


class ThoughtSignatures:
    """Thread-safe record of the tool_use ids this bridge issued."""

    def __init__(self) -> None:
        self._by_id: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def remember(self, tool_use_id: str, signature: str | None) -> None:
        with self._lock:
            self._by_id[tool_use_id] = signature

    def lookup(self, tool_use_id: str) -> tuple[bool, str | None]:
        with self._lock:
            if tool_use_id not in self._by_id:
                return False, None
            return True, self._by_id[tool_use_id]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type", "text") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n\n".join(part for part in parts if part)


def _image_part(block: Mapping[str, Any]) -> dict[str, Any] | None:
    source = block.get("source")
    if not isinstance(source, Mapping):
        return None
    if source.get("type") == "base64" and source.get("data"):
        media_type = str(source.get("media_type") or "image/png")
        url = f"data:{media_type};base64,{source['data']}"
    elif source.get("type") == "url" and source.get("url"):
        url = str(source["url"])
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _tool_result_message(
    block: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the Chat tool message and any images it carried."""
    content = block.get("content")
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "image":
                image = _image_part(part)
                if image is not None:
                    images.append(image)
                    texts.append("[image attached in the next user message]")
            else:
                texts.append(_text([part]))
    else:
        texts.append(_text(content))
    text = "\n\n".join(part for part in texts if part) or "(no output)"
    if block.get("is_error") and not text.lower().startswith("error"):
        text = f"Error: {text}"
    return {
        "role": "tool",
        "tool_call_id": str(block.get("tool_use_id") or ""),
        "content": text,
    }, images


def _user_messages(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return [{"role": "user", "content": _text(content)}]
    # Chat requires tool results directly after the assistant's tool calls, so
    # they go first; any text or images in the same turn follow as a user message.
    tool_messages: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "tool_result":
            message, images = _tool_result_message(block)
            tool_messages.append(message)
            parts.extend(images)
        elif kind == "text" and block.get("text"):
            parts.append({"type": "text", "text": str(block["text"])})
        elif kind == "image":
            image = _image_part(block)
            if image is not None:
                parts.append(image)
        elif kind == "document":
            parts.append({"type": "text", "text": "[document omitted by the bridge]"})
    messages = list(tool_messages)
    if parts:
        if all(part["type"] == "text" for part in parts):
            messages.append({
                "role": "user",
                "content": "\n\n".join(part["text"] for part in parts),
            })
        else:
            messages.append({"role": "user", "content": parts})
    return messages


def _assistant_message(
    content: Any, signatures: ThoughtSignatures
) -> tuple[dict[str, Any], int]:
    """Return the Chat assistant message and how many placeholders it needed."""
    if not isinstance(content, list):
        return {"role": "assistant", "content": _text(content)}, 0
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    any_known = False
    for block in content:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "tool_use":
            call_id = str(block.get("id") or f"toolu_{uuid.uuid4().hex}")
            call: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(
                        block.get("input") or {}, separators=(",", ":")
                    ),
                },
            }
            known, signature = signatures.lookup(call_id)
            any_known = any_known or known
            if signature:
                call["extra_content"] = {"google": {"thought_signature": signature}}
            calls.append(call)
        # Thinking blocks are dropped: the bridge never emits them, and Gemini
        # carries its reasoning state in the thought signatures instead.
    message: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(texts) or None}
    placeholders = 0
    if calls:
        if not any_known:
            # Only the first call of a step carries a signature.
            calls[0]["extra_content"] = {
                "google": {"thought_signature": SIGNATURE_PLACEHOLDER}
            }
            placeholders = 1
        message["tool_calls"] = calls
    return message, placeholders


def _tools(tools: Any) -> list[dict[str, Any]]:
    chat_tools: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return chat_tools
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        # Anthropic server tools (web search, code execution) have a versioned
        # type and no input schema; Vertex cannot run them.
        if tool.get("type") not in (None, "custom") or "input_schema" not in tool:
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        chat_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("input_schema") or {
                    "type": "object",
                    "properties": {},
                },
            },
        })
    return chat_tools


def _tool_choice(choice: Any) -> Any:
    if not isinstance(choice, Mapping):
        return "auto"
    kind = choice.get("type")
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return "auto"


def anthropic_request_to_chat(
    body: Mapping[str, Any],
    config: BridgeConfig,
    *,
    reasoning_effort: str | None,
    signatures: ThoughtSignatures,
) -> tuple[dict[str, Any], int]:
    """Return the Chat request and the number of signature placeholders used."""
    messages: list[dict[str, Any]] = []
    system = _text(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise BridgeError("messages must be an array", status=400)
    placeholders = 0
    for item in raw_messages:
        if not isinstance(item, Mapping):
            continue
        if item.get("role") == "assistant":
            message, used = _assistant_message(item.get("content"), signatures)
            messages.append(message)
            placeholders += used
        else:
            messages.extend(_user_messages(item.get("content")))

    try:
        requested = int(body.get("max_tokens") or config.max_output_tokens)
    except (TypeError, ValueError):
        requested = config.max_output_tokens
    request: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "max_tokens": min(max(1, requested), config.max_output_tokens),
    }
    tools = _tools(body.get("tools"))
    if tools:
        request["tools"] = tools
        request["tool_choice"] = _tool_choice(body.get("tool_choice"))
    for source, target in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(source) is not None:
            request[target] = body[source]
    if body.get("stop_sequences"):
        request["stop"] = list(body["stop_sequences"])
    if reasoning_effort:
        request["reasoning_effort"] = reasoning_effort
    return request, placeholders


def _stop_reason(finish_reason: Any, has_tool_use: bool) -> str:
    if has_tool_use:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


def chat_response_to_message(
    chat: Mapping[str, Any], *, model: str, signatures: ThoughtSignatures
) -> dict[str, Any]:
    choices = chat.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise BridgeError("Vertex returned no Chat Completions choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise BridgeError("Vertex returned a malformed Chat Completions message")

    content: list[dict[str, Any]] = []
    text = _text(message.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
            continue
        function = call["function"]
        arguments = function.get("arguments")
        try:
            tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            tool_input = {"arguments": arguments}
        if not isinstance(tool_input, dict):
            tool_input = {} if tool_input is None else {"arguments": tool_input}
        extra = call.get("extra_content")
        google = extra.get("google") if isinstance(extra, Mapping) else None
        signature = google.get("thought_signature") if isinstance(google, Mapping) else None
        # Gemini's call ids can repeat across turns; Claude Code needs unique ones.
        tool_use_id = f"toolu_{uuid.uuid4().hex}"
        signatures.remember(tool_use_id, str(signature) if signature else None)
        content.append({
            "type": "tool_use",
            "id": tool_use_id,
            "name": str(function.get("name") or ""),
            "input": tool_input,
        })

    usage = chat.get("usage") if isinstance(chat.get("usage"), Mapping) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    prompt_details = usage.get("prompt_tokens_details")
    cached = (
        int(prompt_details.get("cached_tokens") or 0)
        if isinstance(prompt_details, Mapping)
        else 0
    )
    completion_details = usage.get("completion_tokens_details")
    reasoning = (
        int(completion_details.get("reasoning_tokens") or 0)
        if isinstance(completion_details, Mapping)
        else 0
    )
    has_tool_use = any(block["type"] == "tool_use" for block in content)
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(choice.get("finish_reason"), has_tool_use),
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, prompt_tokens - cached),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached,
            "output_tokens": int(usage.get("completion_tokens") or 0) + reasoning,
        },
    }


def message_events(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Create a complete Anthropic SSE event sequence from a buffered message."""
    started = copy.deepcopy(dict(message))
    started["content"] = []
    started["stop_reason"] = None
    started["usage"] = {**message["usage"], "output_tokens": 0}
    events: list[dict[str, Any]] = [{"type": "message_start", "message": started}]
    for index, block in enumerate(message.get("content") or []):
        if block["type"] == "text":
            events.append({
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            })
            events.append({
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": block["text"]},
            })
        else:
            events.append({
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": {},
                },
            })
            events.append({
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"], separators=(",", ":")),
                },
            })
        events.append({"type": "content_block_stop", "index": index})
    events.append({
        "type": "message_delta",
        "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": message["usage"]["output_tokens"]},
    })
    events.append({"type": "message_stop"})
    return events


class AnthropicVertexBridge(AbstractContextManager["AnthropicVertexBridge"]):
    """Run the translation service on an ephemeral localhost TCP port."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        audit_path: Path,
        reasoning_effort: str | None = None,
    ):
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")
        self.config = config
        self.reasoning_effort = reasoning_effort
        self.audit_path = audit_path
        self.bearer_token = secrets.token_urlsafe(32)
        self.signatures = ThoughtSignatures()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._audit_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_count = 0
        self._token_source = GoogleAccessTokenSource()

    @property
    def base_url(self) -> str:
        """Root URL for ANTHROPIC_BASE_URL; Claude Code appends ``/v1/messages``."""
        if self.server is None:
            raise RuntimeError("bridge has not started")
        return f"http://127.0.0.1:{self.server.server_port}"

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
                # Not 429: Claude Code would keep retrying a rate limit.
                raise BridgeError("per-invocation bridge request limit exceeded", status=400)
            return self._request_count

    def __enter__(self) -> "AnthropicVertexBridge":
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
                    self._json(200, {
                        "data": [{
                            "type": "model",
                            "id": bridge.config.model,
                            "display_name": bridge.config.model,
                            "created_at": "1970-01-01T00:00:00Z",
                        }],
                        "has_more": False,
                        "first_id": bridge.config.model,
                        "last_id": bridge.config.model,
                    })
                else:
                    self._error(404, "not found")

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = self._path()
                try:
                    if path == "/v1/messages/count_tokens":
                        # Rough estimate; Vertex has no compatible counting route.
                        body = self._body()
                        self._json(200, {"input_tokens": len(json.dumps(body)) // 4})
                        return
                    if path != "/v1/messages":
                        self._error(404, "not found")
                        return
                    request_number = bridge._next_request()
                    body = self._body()
                    chat_request, placeholders = anthropic_request_to_chat(
                        body,
                        bridge.config,
                        reasoning_effort=bridge.reasoning_effort,
                        signatures=bridge.signatures,
                    )
                    bridge._audit({
                        "event": "request",
                        "request_number": request_number,
                        "message_count": len(chat_request["messages"]),
                        "tool_count": len(chat_request.get("tools") or []),
                        "stream": bool(body.get("stream")),
                        "signature_placeholders": placeholders,
                    })
                    chat_response = client.complete(chat_request)
                    message = chat_response_to_message(
                        chat_response,
                        model=bridge.config.model,
                        signatures=bridge.signatures,
                    )
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for event in message_events(message):
                            payload = json.dumps(event, separators=(",", ":"))
                            self.wfile.write(
                                f"event: {event['type']}\ndata: {payload}\n\n".encode()
                            )
                        self.wfile.flush()
                        self.close_connection = True
                    else:
                        self._json(200, message)
                    bridge._audit({
                        "event": "response",
                        "request_number": request_number,
                        "content_block_count": len(message["content"]),
                        "tool_use_count": sum(
                            block["type"] == "tool_use" for block in message["content"]
                        ),
                        "stop_reason": message["stop_reason"],
                        "input_tokens": message["usage"]["input_tokens"],
                        "cache_read_input_tokens": message["usage"]["cache_read_input_tokens"],
                        "output_tokens": message["usage"]["output_tokens"],
                    })
                except BridgeError as exc:
                    bridge._audit({"event": "bridge_error", "status": exc.status, "error": str(exc)})
                    self._error(exc.status, str(exc))
                except (ValueError, TypeError) as exc:
                    self._error(400, f"invalid request: {exc}")
                except (BrokenPipeError, ConnectionResetError):
                    bridge._audit({"event": "client_disconnected"})
                except Exception as exc:  # fail closed without a server traceback leak
                    bridge._audit({
                        "event": "bridge_error",
                        "status": 500,
                        "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                    })
                    self._error(500, "internal bridge error")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="vertex-gemini-anthropic-bridge",
            daemon=True,
        )
        self.thread.start()
        self._audit({
            "event": "bridge_started",
            "model": self.config.model,
            "location": self.config.location,
            "project_id": self.config.project_id,
            "reasoning_effort": self.reasoning_effort,
        })
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._audit({"event": "bridge_stopped", "request_count": self._request_count})
