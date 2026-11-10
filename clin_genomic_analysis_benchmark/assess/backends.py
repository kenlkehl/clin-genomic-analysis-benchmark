"""Model APIs for the assess task.

A model is named ``<provider>:<model>``:

- ``vertex-anthropic:<model>`` — Claude on Vertex AI (``ANTHROPIC_VERTEX_PROJECT_ID``,
  region from ``CLOUD_ML_REGION`` / ``ANTHROPIC_VERTEX_REGION``, default ``global``).
  The rulebook is sent as a cached prompt block.
- ``vertex-openai:<model>`` — any model on Vertex AI's OpenAI-compatible endpoint
  (Gemini, Gemma, other MaaS models), with Google application-default credentials.
- ``vertex-responses:<publisher>/<model>`` — a partner model that Vertex AI serves
  only through its per-publisher OpenAI Responses endpoint (for example
  ``meta/muse-ember-1.0``), with the same credentials.
- ``azure-openai:<deployment>`` — a GPT deployment on Azure OpenAI's v1 endpoint
  (``AZURE_OPENAI_ENDPOINT``; key from ``AZURE_OPENAI_API_KEY`` or ``az``).
- ``openai-compatible:<model>`` — any OpenAI-compatible server, such as a local vLLM
  or Unsloth Studio server (``--base-url``; optional key from ``--api-key-env``).

Each call sends one user message, the rulebook followed by the question, and
returns the reply text. The harness, not the SDK, retries failed calls.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from .prompt import SEPARATOR, AssessPrompt

PROVIDERS = ("vertex-anthropic", "vertex-openai", "vertex-responses", "azure-openai",
             "openai-compatible")
DEFAULT_ANTHROPIC_MAX_TOKENS = 32_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 900.0


@dataclass
class Completion:
    text: str
    stop_reason: Optional[str] = None
    response_model: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendConfig:
    """Everything needed to rebuild a backend. Never holds a secret: keys come
    from the environment at call time."""

    provider: str
    model: str
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    params: dict[str, Any] = field(default_factory=dict)
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    project: Optional[str] = None
    region: Optional[str] = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BackendConfig":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in raw.items() if key in known})


class Backend(Protocol):
    config: BackendConfig

    def complete(self, prompt: AssessPrompt) -> Completion: ...

    def provenance(self) -> dict[str, Any]: ...


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Split ``provider:model``; the model part may itself contain colons."""
    provider, sep, model = spec.partition(":")
    if not sep or not model:
        raise ValueError(
            f"model must be <provider>:<model>, got {spec!r}; providers: {', '.join(PROVIDERS)}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
    return provider, model


def _redacted_url(url: Optional[str]) -> Optional[str]:
    """Drop credentials and query strings from a URL before recording it."""
    if not url:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _base_provenance(config: BackendConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "model": config.model,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "params": dict(config.params),
        "request_timeout_seconds": config.request_timeout_seconds,
    }


# ---- Claude on Vertex --------------------------------------------------------

class VertexAnthropicBackend:
    def __init__(self, config: BackendConfig):
        from anthropic import AnthropicVertex

        project = config.project or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        if not project:
            raise ValueError("vertex-anthropic needs ANTHROPIC_VERTEX_PROJECT_ID or --project")
        region = (config.region or os.environ.get("CLOUD_ML_REGION")
                  or os.environ.get("ANTHROPIC_VERTEX_REGION") or "global")
        self.config = BackendConfig.from_dict({**config.to_dict(), "project": project,
                                               "region": region})
        self._client = AnthropicVertex(project_id=project, region=region,
                                       timeout=config.request_timeout_seconds, max_retries=0)

    def complete(self, prompt: AssessPrompt) -> Completion:
        content = [
            # The rulebook is the same for every question, so cache it.
            {"type": "text", "text": prompt.rulebook.rstrip("\n") + SEPARATOR,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt.question},
        ]
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
        }
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        kwargs.update(self.config.params)
        # Streaming avoids the SDK's refusal of long non-streaming requests.
        with self._client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
        text = "".join(block.text for block in message.content
                       if getattr(block, "type", None) == "text")
        usage = message.usage
        return Completion(
            text=text,
            stop_reason=message.stop_reason,
            response_model=getattr(message, "model", None),
            usage={
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens":
                    getattr(usage, "cache_creation_input_tokens", 0) or 0,
            },
        )

    def provenance(self) -> dict[str, Any]:
        return {**_base_provenance(self.config), "project": self.config.project,
                "region": self.config.region}


# ---- OpenAI chat-completions family ------------------------------------------

def _chat_completion(client: Any, config: BackendConfig, prompt: AssessPrompt, *,
                     max_tokens_field: str) -> Completion:
    kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt.text}],
    }
    if config.max_tokens is not None:
        kwargs[max_tokens_field] = config.max_tokens
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    kwargs.update(config.params)
    response = client.chat.completions.create(**kwargs)
    if not response.choices:
        return Completion(text="", stop_reason="no_choices",
                          response_model=getattr(response, "model", None))
    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    return Completion(
        text=choice.message.content or "",
        stop_reason=choice.finish_reason,
        response_model=getattr(response, "model", None),
        usage={
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        } if usage is not None else {},
    )


def _response(client: Any, model: str, config: BackendConfig,
              prompt: AssessPrompt) -> Completion:
    """One call to an OpenAI Responses endpoint."""
    kwargs: dict[str, Any] = {"model": model, "input": prompt.text}
    if config.max_tokens is not None:
        kwargs["max_output_tokens"] = config.max_tokens
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    kwargs.update(config.params)
    response = client.responses.create(**kwargs)
    incomplete = getattr(response, "incomplete_details", None)
    stop_reason = getattr(incomplete, "reason", None) or getattr(response, "status", None)
    usage = getattr(response, "usage", None)
    return Completion(
        text=response.output_text or "",
        # A reply cut off by the token limit reports the same reason as Chat Completions.
        stop_reason="length" if stop_reason == "max_output_tokens" else stop_reason,
        response_model=getattr(response, "model", None),
        usage={
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        } if usage is not None else {},
    )


class OpenAICompatibleBackend:
    def __init__(self, config: BackendConfig):
        from openai import OpenAI

        base_url = config.base_url or os.environ.get("CLINGEN_ASSESS_BASE_URL")
        if not base_url:
            raise ValueError("openai-compatible needs --base-url (or CLINGEN_ASSESS_BASE_URL)")
        if config.api_key_env and not os.environ.get(config.api_key_env):
            raise ValueError(f"--api-key-env {config.api_key_env} is not set")
        api_key = os.environ.get(config.api_key_env) if config.api_key_env else "EMPTY"
        self.config = BackendConfig.from_dict({**config.to_dict(), "base_url": base_url})
        self._client = OpenAI(base_url=base_url, api_key=api_key,
                              timeout=config.request_timeout_seconds, max_retries=0)

    def complete(self, prompt: AssessPrompt) -> Completion:
        return _chat_completion(self._client, self.config, prompt, max_tokens_field="max_tokens")

    def provenance(self) -> dict[str, Any]:
        return {**_base_provenance(self.config), "base_url": _redacted_url(self.config.base_url),
                "api_key_env": self.config.api_key_env}


class VertexOpenAIBackend:
    """Vertex AI's OpenAI-compatible endpoint, authenticated with a Google
    access token that is refreshed when it expires."""

    def __init__(self, config: BackendConfig):
        import google.auth

        project = (config.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
                   or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", ""))
        if not project:
            raise ValueError(
                "vertex-openai needs --project, GOOGLE_CLOUD_PROJECT, or "
                "ANTHROPIC_VERTEX_PROJECT_ID")
        region = config.region or "global"
        host = "aiplatform.googleapis.com" if region == "global" else (
            f"{region}-aiplatform.googleapis.com")
        base_url = (f"https://{host}/v1/projects/{project}/locations/{region}"
                    f"{self._endpoint_path(config.model)}")
        self.config = BackendConfig.from_dict({**config.to_dict(), "project": project,
                                               "region": region, "base_url": base_url})
        self._credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._lock = threading.Lock()

    def _token(self) -> str:
        from google.auth.transport.requests import Request

        with self._lock:
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            return self._credentials.token

    def _endpoint_path(self, model: str) -> str:
        return "/endpoints/openapi"

    def _client(self) -> Any:
        from openai import OpenAI

        return OpenAI(base_url=self.config.base_url, api_key=self._token(),
                      timeout=self.config.request_timeout_seconds, max_retries=0)

    def complete(self, prompt: AssessPrompt) -> Completion:
        return _chat_completion(self._client(), self.config, prompt,
                                max_tokens_field="max_tokens")

    def provenance(self) -> dict[str, Any]:
        return {**_base_provenance(self.config), "project": self.config.project,
                "region": self.config.region}


class VertexResponsesBackend(VertexOpenAIBackend):
    """Vertex AI's per-publisher OpenAI Responses endpoint. The model is
    ``<publisher>/<model>``; the request names the bare model."""

    def _endpoint_path(self, model: str) -> str:
        publisher, sep, name = model.partition("/")
        if not sep or not publisher or not name:
            raise ValueError(f"vertex-responses model must be <publisher>/<model>, got {model!r}")
        return f"/publishers/{publisher}/v1"

    def complete(self, prompt: AssessPrompt) -> Completion:
        return _response(self._client(), self.config.model.partition("/")[2], self.config,
                         prompt)


class AzureOpenAIBackend:
    """Azure OpenAI's v1 endpoint, with an Azure AD token refreshed through
    ``az`` when the configured key is missing, expired, or rejected."""

    def __init__(self, config: BackendConfig):
        base_url = config.base_url or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        if not base_url:
            raise ValueError("azure-openai needs AZURE_OPENAI_ENDPOINT or --base-url")
        self.config = BackendConfig.from_dict({**config.to_dict(), "base_url": base_url})
        self._lock = threading.Lock()
        self._api_key: Optional[str] = None

    def _key(self, *, refresh: bool = False) -> str:
        from ..llm.azure_openai_client import _fetch_azure_token, _is_jwt_expired

        with self._lock:
            key = self._api_key or os.environ.get("AZURE_OPENAI_API_KEY", "")
            if refresh or not key or _is_jwt_expired(key):
                fresh = _fetch_azure_token()
                if not fresh:
                    raise RuntimeError(
                        "Azure OpenAI key is missing or expired and `az account "
                        "get-access-token` returned none; run `az login`")
                key = fresh
            self._api_key = key
            return key

    def complete(self, prompt: AssessPrompt) -> Completion:
        from openai import AuthenticationError, OpenAI

        for refresh in (False, True):
            client = OpenAI(base_url=self.config.base_url, api_key=self._key(refresh=refresh),
                            timeout=self.config.request_timeout_seconds, max_retries=0)
            try:
                return _chat_completion(client, self.config, prompt,
                                        max_tokens_field="max_completion_tokens")
            except AuthenticationError:
                if refresh:
                    raise
        raise AssertionError("unreachable")

    def provenance(self) -> dict[str, Any]:
        return {**_base_provenance(self.config), "base_url": _redacted_url(self.config.base_url)}


_BACKENDS = {
    "vertex-anthropic": VertexAnthropicBackend,
    "vertex-openai": VertexOpenAIBackend,
    "vertex-responses": VertexResponsesBackend,
    "azure-openai": AzureOpenAIBackend,
    "openai-compatible": OpenAICompatibleBackend,
}


def make_backend(config: BackendConfig) -> Backend:
    if config.provider not in _BACKENDS:
        raise ValueError(f"unknown provider {config.provider!r}")
    return _BACKENDS[config.provider](config)
