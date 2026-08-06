"""Codex CLI adapter for Gemma 4 26B on Google Vertex Agent Platform."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
from adapters.codex_vertex_gemma4_26b.vertex_bridge import (  # noqa: E402
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    BridgeConfig,
    VertexResponsesBridge,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)


PROVIDER_NAME = "google_vertex_agent_platform"
MODEL_CONTEXT_WINDOW = 262_144


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required; set it to the Google Cloud project with "
            "Vertex Agent Platform Gemma access"
        )
    return value


def _bridge_config() -> BridgeConfig:
    project_id = _required_env("VERTEX_GEMMA_PROJECT_ID")
    location = os.environ.get("VERTEX_GEMMA_LOCATION", DEFAULT_LOCATION).strip()
    model = os.environ.get("VERTEX_GEMMA_MODEL", DEFAULT_MODEL).strip()
    if not location or "/" in location:
        raise RuntimeError("VERTEX_GEMMA_LOCATION must be a location name")
    if not project_id or "/" in project_id:
        raise RuntimeError("VERTEX_GEMMA_PROJECT_ID must be a Google Cloud project ID")
    if model != DEFAULT_MODEL:
        raise RuntimeError(
            f"this adapter is pinned to {DEFAULT_MODEL}; got VERTEX_GEMMA_MODEL={model}"
        )
    return BridgeConfig(
        project_id=project_id,
        location=location,
        model=model,
        request_timeout_seconds=_env_float(
            "VERTEX_GEMMA_REQUEST_TIMEOUT_SECONDS", 600.0, minimum=1.0
        ),
        max_retries=_env_int("VERTEX_GEMMA_MAX_RETRIES", 6),
        retry_base_seconds=_env_float(
            "VERTEX_GEMMA_RETRY_BASE_SECONDS", 5.0, minimum=0.0
        ),
        max_retry_sleep_seconds=_env_float(
            "VERTEX_GEMMA_MAX_RETRY_SLEEP_SECONDS", 60.0, minimum=0.0
        ),
        max_output_tokens=_env_int("VERTEX_GEMMA_MAX_OUTPUT_TOKENS", 16_384),
        max_requests=_env_int("VERTEX_GEMMA_MAX_REQUESTS", 256),
    )


def _save_attempt_logs(
    *, scratch_dir: Path, stage: str, attempt: int, proc: subprocess.CompletedProcess[str]
) -> None:
    if not _env_truthy("CODEX_SAVE_ATTEMPT_LOGS", True):
        return
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / f"codex_vertex_attempt.{stage}.{attempt}.stdout.txt").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"codex_vertex_attempt.{stage}.{attempt}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _provider_override(key: str, value: str) -> list[str]:
    return ["-c", f"model_providers.{PROVIDER_NAME}.{key}={json.dumps(value)}"]


def _codex_call(*, prompt: str, question: dict, last_message_file: Path) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("CODEX_MAX_ATTEMPTS", 1)
    retry_sleep = _env_float("CODEX_RETRY_BASE_SECONDS", 15.0)
    config = _bridge_config()

    codex_bin = os.environ.get("CODEX_BIN", "codex")
    base_cmd = [
        codex_bin,
        "exec",
        "-C",
        str(SANDBOX_SCRATCH_DIR),
        "--add-dir",
        str(SANDBOX_COHORT_DIR),
        "--skip-git-repo-check",
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
        base_cmd.append("--ephemeral")
    base_cmd += [
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
            / f"vertex_bridge.{stage}.{attempt}.jsonl"
        )
        with VertexResponsesBridge(config, audit_path=audit_path) as bridge:
            cmd = [*base_cmd]
            cmd += _provider_override("name", "Google Vertex Agent Platform (Gemma bridge)")
            cmd += _provider_override("base_url", bridge.base_url)
            cmd += _provider_override("env_key", "VERTEX_GEMMA_BRIDGE_KEY")
            cmd += _provider_override("wire_api", "responses")
            cmd += [
                "-c",
                f"model_providers.{PROVIDER_NAME}.request_max_retries=0",
                "-c",
                f"model_providers.{PROVIDER_NAME}.stream_max_retries=0",
                "-",
            ]
            env = os.environ.copy()
            env["CODEX_MODEL"] = config.model
            env["CODEX_MODEL_PROVIDER"] = PROVIDER_NAME
            env.pop("CODEX_REASONING_EFFORT", None)
            env["VERTEX_GEMMA_BRIDGE_KEY"] = bridge.bearer_token

            with sandboxed_agent_command(
                cmd,
                cohort_dir=cohort_dir,
                data_dictionary_path=question["data_dictionary_path"],
                scratch_dir=scratch_dir,
                environment=env,
                home_kind="codex_vertex",
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
                        / f"codex_vertex_attempt_{stage}_{attempt}"
                    ),
                    home_kind="codex_vertex",
                )

        _save_attempt_logs(
            scratch_dir=scratch_dir,
            stage=stage,
            attempt=attempt,
            proc=proc,
        )
        if proc.returncode == 0:
            text = last_message_file.read_text().strip() if last_message_file.exists() else ""
            return text or proc.stdout

        stderr = proc.stderr or ""
        last_error = f"codex exited {proc.returncode}: {stderr[-8000:]}"
        if attempt >= max_attempts or not _is_retryable_codex_failure(stderr):
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
        sys.stderr.write(f"adapter: Codex/Vertex invocation failed: {exc}\n")
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
