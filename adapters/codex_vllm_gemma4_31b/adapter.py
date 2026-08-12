"""Codex CLI adapter for Gemma 4 31B on an OpenAI-compatible vLLM server."""

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
    _is_retryable_codex_failure,
    _load_question,
    _sandbox_for,
)
from adapters.codex_vllm_gemma4_31b.vllm_bridge import (  # noqa: E402
    BridgeConfig,
    VLLMResponsesBridge,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)


PROVIDER_NAME = "local_vllm_gemma4_31b"
DEFAULT_MODEL = "gemma4-31b"
DEFAULT_BASE_URL = "http://camus.dfci.harvard.edu:8002/v1"
MODEL_CONTEXT_WINDOW = 262_144
TOKEN_ENV = "VLLM_TOKEN"
BRIDGE_TOKEN_ENV = "VLLM_GEMMA_BRIDGE_KEY"


def _model() -> str:
    model = (
        os.environ.get("VLLM_MODEL", "").strip()
        or os.environ.get("CODEX_MODEL", "").strip()
        or DEFAULT_MODEL
    )
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


def _provider_override(key: str, value: str) -> list[str]:
    return ["-c", f"model_providers.{PROVIDER_NAME}.{key}={json.dumps(value)}"]


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


def _save_attempt_logs(
    *,
    scratch_dir: Path,
    stage: str,
    attempt: int,
    proc: subprocess.CompletedProcess[str],
) -> None:
    if not _env_truthy("CODEX_SAVE_ATTEMPT_LOGS", True):
        return
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / f"codex_vllm_attempt.{stage}.{attempt}.stdout.txt").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"codex_vllm_attempt.{stage}.{attempt}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _is_retryable_vllm_failure(stderr: str) -> bool:
    low = stderr.lower()
    return _is_retryable_codex_failure(stderr) or any(
        term in low
        for term in (
            "connection refused",
            "connection timed out",
            "dns error",
            "error sending request",
            "502 bad gateway",
            "504 gateway timeout",
        )
    )


def _codex_call(*, prompt: str, question: dict, last_message_file: Path) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("CODEX_MAX_ATTEMPTS", 1)
    retry_sleep = _env_float("CODEX_RETRY_BASE_SECONDS", 15.0)
    config = _bridge_config()

    codex_bin = os.environ.get("CODEX_BIN", "codex")
    cmd = [
        codex_bin,
        "exec",
        "-C",
        str(SANDBOX_SCRATCH_DIR),
        "--add-dir",
        str(SANDBOX_COHORT_DIR),
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox",
        _sandbox_for(stage),
        "-c",
        'approval_policy="never"',
        "--color",
        "never",
        "-o",
        str(SANDBOX_SCRATCH_DIR / last_message_file.name),
    ]
    if _env_truthy("CODEX_EPHEMERAL", True):
        cmd.append("--ephemeral")
    cmd += [
        "-c",
        f"model_provider={json.dumps(PROVIDER_NAME)}",
        "--model",
        config.model,
        "-c",
        f"model_context_window={MODEL_CONTEXT_WINDOW}",
    ]

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        if last_message_file.exists():
            last_message_file.unlink()
        audit_path = (
            scratch_dir.parent
            / "adapter_audit"
            / f"vllm_bridge.{stage}.{attempt}.jsonl"
        )
        with VLLMResponsesBridge(config, audit_path=audit_path) as bridge:
            attempt_cmd = [*cmd]
            attempt_cmd += _provider_override(
                "name", "Local vLLM (Gemma 4 31B compatibility bridge)"
            )
            attempt_cmd += _provider_override("base_url", bridge.base_url)
            attempt_cmd += _provider_override("env_key", BRIDGE_TOKEN_ENV)
            attempt_cmd += _provider_override("wire_api", "responses")
            attempt_cmd += [
                "-c",
                f"model_providers.{PROVIDER_NAME}.requires_openai_auth=false",
                "-c",
                f"model_providers.{PROVIDER_NAME}.request_max_retries=0",
                "-c",
                f"model_providers.{PROVIDER_NAME}.stream_max_retries=0",
                "-",
            ]
            env = os.environ.copy()
            env["CODEX_MODEL"] = config.model
            env["CODEX_MODEL_PROVIDER"] = PROVIDER_NAME
            env[BRIDGE_TOKEN_ENV] = bridge.bearer_token
            env.pop("CODEX_REASONING_EFFORT", None)
            env.pop(TOKEN_ENV, None)

            with sandboxed_agent_command(
                attempt_cmd,
                cohort_dir=cohort_dir,
                data_dictionary_path=question["data_dictionary_path"],
                scratch_dir=scratch_dir,
                environment=env,
                home_kind="codex_vllm",
            ) as launch:
                proc = subprocess.run(
                    launch.command,
                    input=prompt,
                    env=launch.environment,
                    capture_output=True,
                    text=True,
                )
                export_agent_session_audit(
                    launch,
                    destination=(
                        scratch_dir.parent
                        / "agent_session_audit"
                        / f"codex_vllm_attempt_{stage}_{attempt}"
                    ),
                    home_kind="codex_vllm",
                )

        _save_attempt_logs(
            scratch_dir=scratch_dir,
            stage=stage,
            attempt=attempt,
            proc=proc,
        )
        if proc.returncode == 0:
            text = (
                last_message_file.read_text().strip()
                if last_message_file.exists()
                else ""
            )
            return text or proc.stdout

        stderr = proc.stderr or ""
        last_error = f"codex exited {proc.returncode}: {stderr[-8000:]}"
        if attempt >= max_attempts or not _is_retryable_vllm_failure(stderr):
            break
        sleep_for = retry_sleep * attempt
        sys.stderr.write(
            f"adapter: codex attempt {attempt}/{max_attempts} failed retryably; "
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
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    last_message_file = scratch_dir / f".codex_last_message.{stage}.txt"
    try:
        text = _codex_call(
            prompt=_build_prompt(question),
            question=question,
            last_message_file=last_message_file,
        )
    except Exception as exc:
        sys.stderr.write(f"adapter: Codex/vLLM invocation failed: {exc}\n")
        return 3

    obj = _extract_json(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from Codex output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
