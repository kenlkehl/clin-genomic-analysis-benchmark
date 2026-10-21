"""OpenCode adapter for OpenAI GPT deployments on Azure OpenAI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ADAPTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = ADAPTER_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.codex_gpt.adapter import (  # noqa: E402
    _build_prompt,
    _env_float,
    _env_int,
    _env_truthy,
    _extract_json,
    _load_question,
)
from adapters.opencode_azure_gpt.azure_bridge import (  # noqa: E402
    AzureResponsesBridge,
    BridgeConfig,
)
from adapters.opencode_vllm_gemma4.adapter import (  # noqa: E402
    _RUNTIME_ENV_ALLOWLIST,
    _final_text_from_events,
    _is_retryable_opencode_failure,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_SCRATCH_DIR,
    sandboxed_agent_command,
)


PROVIDER_NAME = "azure_openai_bridge"
ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
MODEL_ENV = "OPENCODE_AZURE_MODEL"
EFFORT_ENV = "OPENCODE_AZURE_REASONING_EFFORT"
DEFAULT_CONTEXT_WINDOW = 272_000
DEFAULT_MAX_OUTPUT_TOKENS = 128_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 1800.0
BRIDGE_TOKEN_ENV = "OPENCODE_AZURE_BRIDGE_KEY"
CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"
OUTPUT_TOKEN_MAX_ENV = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"
# The values accepted by OpenCode's bundled AI SDK for Responses models.
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


def _model() -> str:
    model = os.environ.get(MODEL_ENV, "").strip()
    if not model:
        raise RuntimeError(f"{MODEL_ENV} must name the Azure OpenAI deployment")
    if any(character.isspace() or character == "/" for character in model):
        raise RuntimeError(f"{MODEL_ENV} must be a single deployment name")
    return model


def _reasoning_effort() -> str | None:
    effort = os.environ.get(EFFORT_ENV, "").strip().lower()
    if not effort:
        return None
    if effort not in REASONING_EFFORTS:
        raise RuntimeError(
            f"{EFFORT_ENV} must be one of {', '.join(sorted(REASONING_EFFORTS))}"
        )
    return effort


def _endpoint() -> str:
    """Return the Azure OpenAI v1 API root, adding ``/openai/v1`` if absent."""
    raw = os.environ.get(ENDPOINT_ENV, "").strip().rstrip("/")
    if not raw:
        raise RuntimeError(f"{ENDPOINT_ENV} must be set to the Azure OpenAI resource URL")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{ENDPOINT_ENV} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{ENDPOINT_ENV} must not contain credentials or a query")
    if not parsed.path.rstrip("/").endswith("/openai/v1"):
        raw += "/openai/v1"
    return raw


def _request_timeout_seconds() -> float:
    return _env_float(
        "OPENCODE_AZURE_REQUEST_TIMEOUT_SECONDS",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        minimum=1.0,
    )


def _bridge_config() -> BridgeConfig:
    return BridgeConfig(
        endpoint=_endpoint(),
        model=_model(),
        request_timeout_seconds=_request_timeout_seconds(),
        max_retries=_env_int("OPENCODE_AZURE_MAX_RETRIES", 6),
        retry_base_seconds=_env_float(
            "OPENCODE_AZURE_RETRY_BASE_SECONDS", 5.0, minimum=0.0
        ),
        max_retry_sleep_seconds=_env_float(
            "OPENCODE_AZURE_MAX_RETRY_SLEEP_SECONDS", 60.0, minimum=0.0
        ),
        max_requests=_env_int("OPENCODE_AZURE_MAX_REQUESTS", 512),
    )


def _max_output_tokens() -> int:
    return _env_int("OPENCODE_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)


def _model_options() -> dict:
    """Provider options for the Responses call.

    OpenCode applies its GPT reasoning defaults only to model names containing
    ``gpt-5``, and its AI SDK treats names outside ``o*``/``gpt-5*``/``codex-*``
    as non-reasoning models, silently dropping the reasoning effort. Set the
    equivalent options explicitly: stateless requests that carry encrypted
    reasoning between turns, as OpenCode does for GPT-5.
    """
    options: dict = {
        "forceReasoning": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoningSummary": "auto",
    }
    effort = _reasoning_effort()
    if effort is not None:
        options["reasoningEffort"] = effort
    return options


def _opencode_config(*, bridge_url: str, max_output_tokens: int) -> str:
    """Build the complete disposable OpenCode configuration for one stage."""
    model = _model()
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": f"{PROVIDER_NAME}/{model}",
        "small_model": f"{PROVIDER_NAME}/{model}",
        "provider": {
            PROVIDER_NAME: {
                # The OpenAI AI SDK provider speaks /v1/responses, which the
                # trusted bridge forwards to Azure with the Entra token.
                "npm": "@ai-sdk/openai",
                "name": "Azure OpenAI (credential bridge)",
                "options": {
                    "baseURL": bridge_url,
                    "apiKey": f"{{env:{BRIDGE_TOKEN_ENV}}}",
                    "timeout": int(_request_timeout_seconds() * 1000),
                },
                "models": {
                    model: {
                        "name": model,
                        "reasoning": True,
                        "tool_call": True,
                        "temperature": False,
                        "limit": {
                            "context": _env_int(
                                "OPENCODE_AZURE_CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW
                            ),
                            "output": max_output_tokens,
                        },
                        "options": _model_options(),
                    }
                },
            }
        },
        "enabled_providers": [PROVIDER_NAME],
        "permission": {
            "*": "allow",
            "task": "deny",
            "skill": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "question": "deny",
        },
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "mcp": {},
        "instructions": [],
    }, separators=(",", ":"))


def _opencode_environment(*, bridge_url: str, bridge_token: str) -> dict[str, str]:
    """Return a credential-free environment for the model-controlled CLI."""
    env = {
        name: os.environ[name]
        for name in _RUNTIME_ENV_ALLOWLIST
        if os.environ.get(name)
    }
    no_proxy = env.get("NO_PROXY", env.get("no_proxy", ""))
    entries = [entry.strip() for entry in no_proxy.split(",") if entry.strip()]
    for local_name in ("127.0.0.1", "localhost"):
        if local_name not in entries:
            entries.append(local_name)
    env["NO_PROXY"] = ",".join(entries)
    env["no_proxy"] = env["NO_PROXY"]
    max_output_tokens = _max_output_tokens()
    # OpenCode caps output at 32,000 tokens unless told otherwise. Reasoning
    # tokens count against that cap, so raise it to the configured limit.
    env[OUTPUT_TOKEN_MAX_ENV] = str(max_output_tokens)
    env[BRIDGE_TOKEN_ENV] = bridge_token
    env[CONFIG_ENV] = _opencode_config(
        bridge_url=bridge_url, max_output_tokens=max_output_tokens
    )
    return env


def _save_attempt_logs(
    *,
    scratch_dir: Path,
    stage: str,
    attempt: int,
    proc: subprocess.CompletedProcess[str],
) -> None:
    if not _env_truthy("OPENCODE_SAVE_ATTEMPT_LOGS", True):
        return
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / f"opencode_azure_attempt.{stage}.{attempt}.stdout.jsonl").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"opencode_azure_attempt.{stage}.{attempt}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _opencode_call(*, prompt: str, question: dict) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("OPENCODE_MAX_ATTEMPTS", 1)
    retry_sleep = _env_float("OPENCODE_RETRY_BASE_SECONDS", 15.0)
    config = _bridge_config()
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
    agent = os.environ.get("OPENCODE_AGENT", "build").strip() or "build"

    cmd = [
        opencode_bin,
        "run",
        "--pure",
        "--format",
        "json",
        "--model",
        f"{PROVIDER_NAME}/{config.model}",
        "--agent",
        agent,
        "--dir",
        str(SANDBOX_SCRATCH_DIR),
        "--title",
        f"clingen-bench:{question['question_id']}:{stage}",
    ]

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        audit_path = (
            scratch_dir.parent
            / "adapter_audit"
            / f"opencode_azure_bridge.{stage}.{attempt}.jsonl"
        )
        with AzureResponsesBridge(config, audit_path=audit_path) as bridge:
            # The trusted adapter keeps the Azure token. OpenCode gets only
            # non-secret runtime settings and the short-lived local key.
            env = _opencode_environment(
                bridge_url=bridge.base_url,
                bridge_token=bridge.bearer_token,
            )

            with sandboxed_agent_command(
                cmd,
                cohort_dir=cohort_dir,
                data_dictionary_path=question["data_dictionary_path"],
                scratch_dir=scratch_dir,
                environment=env,
                home_kind="opencode_azure",
            ) as launch:
                proc = subprocess.run(
                    launch.command,
                    input=prompt,
                    env=launch.environment,
                    capture_output=True,
                    text=True,
                )

        _save_attempt_logs(
            scratch_dir=scratch_dir,
            stage=stage,
            attempt=attempt,
            proc=proc,
        )
        if proc.returncode == 0:
            return _final_text_from_events(proc.stdout)

        stderr = proc.stderr or ""
        last_error = f"opencode exited {proc.returncode}: {stderr[-8000:]}"
        if attempt >= max_attempts or not _is_retryable_opencode_failure(stderr):
            break
        sleep_for = retry_sleep * attempt
        sys.stderr.write(
            f"adapter: opencode attempt {attempt}/{max_attempts} failed retryably; "
            f"retrying in {sleep_for:.1f}s\n"
        )
        time.sleep(sleep_for)
    raise RuntimeError(last_error)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    question = _load_question(args.question_file)
    try:
        text = _opencode_call(prompt=_build_prompt(question), question=question)
    except Exception as exc:
        sys.stderr.write(f"adapter: OpenCode/Azure invocation failed: {exc}\n")
        return 3

    obj = _extract_json(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from OpenCode output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
