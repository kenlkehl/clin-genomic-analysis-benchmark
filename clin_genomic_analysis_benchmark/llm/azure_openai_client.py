"""Azure OpenAI client (BAA-covered) used for the OpenAI-side reviewer/judge.

Uses the openai SDK's plain `OpenAI()` client pointed at the Azure v1 endpoint
(matches the user's existing setup at
`AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1`) and an
Azure AD bearer token as the api_key.

Auto-refreshes the bearer token via `az account get-access-token` when:
  - the constructor finds AZURE_OPENAI_API_KEY missing or expired,
  - or a request returns 401 Unauthorized.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from openai import OpenAI
from openai import APIStatusError, AuthenticationError, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import AzureConfig

logger = logging.getLogger(__name__)

_AZURE_RESOURCE = "https://cognitiveservices.azure.com/"
_TOKEN_REFRESH_CMD = [
    "az", "account", "get-access-token",
    f"--resource={_AZURE_RESOURCE}",
    "--query", "accessToken",
    "--output", "tsv",
]
_TOKEN_REFRESH_LOCK = threading.Lock()


def _decode_jwt_exp(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _is_jwt_expired(token: str, slack_seconds: int = 60) -> bool:
    exp = _decode_jwt_exp(token)
    if exp is None:
        # Not a JWT — treat as opaque key, assume valid
        return False
    return exp <= int(time.time()) + slack_seconds


def _fetch_azure_token() -> Optional[str]:
    """Run `az account get-access-token` to fetch a fresh bearer. Returns None on failure."""
    if not shutil.which("az"):
        logger.warning("`az` CLI not found on PATH; cannot auto-refresh Azure AD token")
        return None
    try:
        with _TOKEN_REFRESH_LOCK:
            proc = subprocess.run(_TOKEN_REFRESH_CMD, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            logger.warning("`az` token refresh failed (exit %d): %s",
                           proc.returncode, proc.stderr.strip())
            return None
        token = proc.stdout.strip()
        return token or None
    except Exception:
        logger.warning("`az` token refresh raised", exc_info=True)
        return None


@dataclass
class JudgeMessage:
    role: str               # "system" | "user" | "assistant"
    content: str


@dataclass
class AzureResponse:
    text: str
    input_tokens: int
    output_tokens: int
    raw: Any = None


class AzureClient:
    """Azure OpenAI wrapper.

    Tasks: gold-script reviewer (sees script + spec, no cohort rows) and
    disambiguation judge (sees question text + gold concepts + agent concepts).
    """

    def __init__(self, config: AzureConfig):
        if not config.endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is empty. Set it in your env or .env file.")
        self.config = config
        self._lock = threading.Lock()
        api_key = config.api_key
        if not api_key or _is_jwt_expired(api_key):
            if api_key and _is_jwt_expired(api_key):
                logger.info("AZURE_OPENAI_API_KEY is expired; refreshing via `az`")
            else:
                logger.info("AZURE_OPENAI_API_KEY is empty; fetching via `az`")
            new = _fetch_azure_token()
            if not new:
                raise RuntimeError(
                    "Azure OpenAI bearer token is missing/expired and `az account get-access-token` "
                    "did not return a fresh token. Run `az login` and retry."
                )
            api_key = new
            os.environ["AZURE_OPENAI_API_KEY"] = api_key
        self._client = OpenAI(base_url=config.endpoint, api_key=api_key)

    def _refresh_token_in_place(self) -> bool:
        """Fetch a new bearer and rebind the underlying client."""
        with self._lock:
            new = _fetch_azure_token()
            if not new:
                return False
            self._client = OpenAI(base_url=self.config.endpoint, api_key=new)
            os.environ["AZURE_OPENAI_API_KEY"] = new
            logger.info("Azure AD token refreshed in-place")
            return True

    @classmethod
    def from_env(cls) -> "AzureClient":
        return cls(AzureConfig.from_env())

    def _create_with_auth_retry(self, kwargs: dict) -> Any:
        """Call responses.create; on 401, refresh the token once and retry."""
        try:
            return self._client.responses.create(**kwargs)
        except AuthenticationError:
            logger.warning("Azure 401 — refreshing bearer token and retrying once")
            if not self._refresh_token_in_place():
                raise
            return self._client.responses.create(**kwargs)
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 401:
                logger.warning("Azure 401 (APIStatusError) — refreshing token and retrying once")
                if not self._refresh_token_in_place():
                    raise
                return self._client.responses.create(**kwargs)
            raise

    @retry(
        retry=retry_if_exception_type((RateLimitError,)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def generate(
        self,
        *,
        system_text: Optional[str] = None,
        user_text: str,
        max_tokens: int = 8000,
        temperature: Optional[float] = None,
        deployment: Optional[str] = None,
    ) -> AzureResponse:
        """Single-turn generation via the Responses API.

        Temperature is opt-in (None=omit). Some Azure deployments (e.g. gpt-5)
        reject `temperature` as deprecated.
        """
        kwargs: dict[str, Any] = {
            "model": deployment or self.config.deployment,
            "input": user_text,
            "max_output_tokens": max_tokens,
        }
        if system_text:
            kwargs["instructions"] = system_text
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = self._create_with_auth_retry(kwargs)
        text = resp.output_text or ""
        usage = getattr(resp, "usage", None)
        return AzureResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            raw=resp,
        )

    @retry(
        retry=retry_if_exception_type((RateLimitError,)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def generate_messages(
        self,
        *,
        messages: list[JudgeMessage],
        max_tokens: int = 8000,
        temperature: Optional[float] = None,
        deployment: Optional[str] = None,
    ) -> AzureResponse:
        """Multi-turn generation. Translates JudgeMessage list into Responses API inputs."""
        input_items = [{"role": m.role, "content": m.content} for m in messages]
        kwargs: dict[str, Any] = {
            "model": deployment or self.config.deployment,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = self._create_with_auth_retry(kwargs)
        text = resp.output_text or ""
        usage = getattr(resp, "usage", None)
        return AzureResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            raw=resp,
        )
