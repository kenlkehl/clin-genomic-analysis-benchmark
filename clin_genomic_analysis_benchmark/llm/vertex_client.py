"""Anthropic Claude on Vertex client with prompt caching + tenacity retries.

Why a thin wrapper rather than the raw SDK: the benchmark sends the same large
"cohort context" (data dictionary + sampled tables) across many question-gen and
gold-codegen calls. We want explicit control over which content blocks are
cached so the cache hit rate stays high.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from anthropic import AnthropicVertex
from anthropic._exceptions import APIError, APIStatusError, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import ClaudeConfig

logger = logging.getLogger(__name__)


@dataclass
class CachedBlock:
    """A text block to mark with cache_control: ephemeral.

    Claude on Vertex supports up to 4 cache breakpoints; the last block carrying
    cache_control caches everything up to and including that block.
    """
    text: str
    label: str = ""        # diagnostic only — not sent to API


@dataclass
class ClaudeResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    raw: Any = None


def _build_system_blocks(cached_blocks: Iterable[CachedBlock], system_text: Optional[str]) -> list[dict]:
    """Assemble the `system` parameter as content blocks with cache_control on the cached ones."""
    blocks: list[dict] = []
    if system_text:
        blocks.append({"type": "text", "text": system_text})
    for cb in cached_blocks:
        blocks.append({
            "type": "text",
            "text": cb.text,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


class VertexClient:
    """Claude (Vertex) wrapper with prompt caching.

    Usage:
        client = VertexClient.from_env()
        resp = client.generate(
            system_text="You are a benchmark designer...",
            cached_blocks=[CachedBlock(text=long_dict_md, label="dictionary")],
            user_text="Generate 5 demographics questions for cohort X.",
        )
    """

    def __init__(self, config: ClaudeConfig):
        if not config.project_id:
            raise RuntimeError(
                "ANTHROPIC_VERTEX_PROJECT_ID is empty. Set it in your env or .env file."
            )
        self.config = config
        self._client = AnthropicVertex(project_id=config.project_id, region=config.region)

    @classmethod
    def from_env(cls) -> "VertexClient":
        return cls(ClaudeConfig.from_env())

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIStatusError, APIError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def generate(
        self,
        *,
        system_text: Optional[str] = None,
        cached_blocks: Optional[list[CachedBlock]] = None,
        user_text: str,
        max_tokens: int = 8000,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Single-turn generation. Caches everything in `cached_blocks`.

        Temperature is opt-in (None = omit). Newer Claude models (Opus 4.7+)
        reject the `temperature` parameter as deprecated.
        """
        cached_blocks = cached_blocks or []
        system_param = _build_system_blocks(cached_blocks, system_text)

        kwargs = {
            "model": model or self.config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_text}],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if system_param:
            kwargs["system"] = system_param

        resp = self._client.messages.create(**kwargs)

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = resp.usage
        return ClaudeResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            raw=resp,
        )

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIStatusError, APIError)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def generate_messages(
        self,
        *,
        system_text: Optional[str] = None,
        cached_blocks: Optional[list[CachedBlock]] = None,
        messages: list[dict],
        max_tokens: int = 8000,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Multi-turn generation using a pre-built messages list."""
        cached_blocks = cached_blocks or []
        system_param = _build_system_blocks(cached_blocks, system_text)
        kwargs = {
            "model": model or self.config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if system_param:
            kwargs["system"] = system_param
        resp = self._client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = resp.usage
        return ClaudeResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            raw=resp,
        )
