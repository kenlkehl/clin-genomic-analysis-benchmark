"""OpenCode adapter for Gemma 4 31B on an OpenAI-compatible vLLM server."""

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
from adapters.codex_vllm_gemma4_31b.vllm_bridge import (  # noqa: E402
    BridgeConfig,
    VLLMResponsesBridge,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_SCRATCH_DIR,
    sandboxed_agent_command,
)


PROVIDER_NAME = "local_vllm_gemma4_31b"
DEFAULT_MODEL = "gemma4-31b"
DEFAULT_BASE_URL = "http://camus.dfci.harvard.edu:8002/v1"
MODEL_CONTEXT_WINDOW = 262_144
DEFAULT_MAX_OUTPUT_TOKENS = 32_768
TOKEN_ENV = "VLLM_TOKEN"
BRIDGE_TOKEN_ENV = "OPENCODE_VLLM_BRIDGE_KEY"
CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"
_RUNTIME_ENV_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
    "NO_COLOR",
}


def _model() -> str:
    model = os.environ.get("VLLM_MODEL", "").strip() or DEFAULT_MODEL
    if any(character.isspace() for character in model):
        raise RuntimeError("VLLM_MODEL must be a single model identifier")
    return model


def _base_url() -> str:
    """Return a validated OpenAI-compatible API root, adding ``/v1`` if absent."""
    raw = os.environ.get("VLLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("VLLM_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise RuntimeError("put vLLM credentials in VLLM_TOKEN, not VLLM_BASE_URL")
    if parsed.query or parsed.fragment:
        raise RuntimeError("VLLM_BASE_URL must not contain a query or fragment")
    if not parsed.path.rstrip("/").endswith("/v1"):
        raw += "/v1"
    return raw


def _bridge_config() -> BridgeConfig:
    return BridgeConfig(
        base_url=_base_url(),
        model=_model(),
        api_key=os.environ.get(TOKEN_ENV, "").strip() or None,
        request_timeout_seconds=_env_float(
            "VLLM_REQUEST_TIMEOUT_SECONDS", 600.0, minimum=1.0
        ),
        max_retries=_env_int("VLLM_MAX_RETRIES", 3),
        retry_base_seconds=_env_float(
            "VLLM_RETRY_BASE_SECONDS", 2.0, minimum=0.0
        ),
        max_retry_sleep_seconds=_env_float(
            "VLLM_MAX_RETRY_SLEEP_SECONDS", 30.0, minimum=0.0
        ),
        max_requests=_env_int("VLLM_MAX_REQUESTS", 256),
    )


def _opencode_config(*, bridge_url: str, max_output_tokens: int) -> str:
    """Build the complete disposable OpenCode configuration for one stage."""
    model = _model()
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": f"{PROVIDER_NAME}/{model}",
        "small_model": f"{PROVIDER_NAME}/{model}",
        "provider": {
            PROVIDER_NAME: {
                # The OpenAI AI SDK provider uses /v1/responses, which is what
                # the local Gemma tool-call compatibility bridge implements.
                "npm": "@ai-sdk/openai",
                "name": "Local vLLM (Gemma 4 31B compatibility bridge)",
                "options": {
                    "baseURL": bridge_url,
                    "apiKey": f"{{env:{BRIDGE_TOKEN_ENV}}}",
                    "timeout": int(
                        _env_float(
                            "VLLM_REQUEST_TIMEOUT_SECONDS", 600.0, minimum=1.0
                        )
                        * 1000
                    ),
                },
                "models": {
                    model: {
                        "name": "Gemma 4 31B",
                        "limit": {
                            "context": MODEL_CONTEXT_WINDOW,
                            "output": max_output_tokens,
                        },
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
    env[BRIDGE_TOKEN_ENV] = bridge_token
    env[CONFIG_ENV] = _opencode_config(
        bridge_url=bridge_url,
        max_output_tokens=_env_int(
            "OPENCODE_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
        ),
    )
    return env


def _final_text_from_events(stdout: str) -> str:
    """Extract the last assistant text step from OpenCode's JSONL output."""
    latest_text = ""
    current_parts: list[str] = []
    saw_event = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "step_start":
            saw_event = True
            current_parts = []
            continue
        if event_type == "text":
            saw_event = True
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                current_parts.append(part["text"])
            continue
        if event_type == "step_finish" and current_parts:
            latest_text = "".join(current_parts).strip()
    if current_parts:
        latest_text = "".join(current_parts).strip()
    return latest_text if saw_event and latest_text else stdout


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
    (audit_dir / f"opencode_vllm_attempt.{stage}.{attempt}.stdout.jsonl").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"opencode_vllm_attempt.{stage}.{attempt}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _is_retryable_opencode_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(
        term in low
        for term in (
            "connection refused",
            "connection reset",
            "connection timed out",
            "dns error",
            "fetch failed",
            "temporarily unavailable",
            "rate limit",
            "429",
            "502 bad gateway",
            "503",
            "504 gateway timeout",
        )
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
            / f"opencode_vllm_bridge.{stage}.{attempt}.jsonl"
        )
        with VLLMResponsesBridge(config, audit_path=audit_path) as bridge:
            # The trusted adapter retains all host credentials. OpenCode gets
            # only non-secret runtime settings and the short-lived local key.
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
                home_kind="opencode_vllm",
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
        sys.stderr.write(f"adapter: OpenCode/vLLM invocation failed: {exc}\n")
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
