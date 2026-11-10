"""Codex CLI adapter for Qwen 3.6 35B-A3B on local Unsloth Studio."""

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
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from adapters.codex_gpt.adapter import (  # noqa: E402
    TASK,
    _build_prompt,
    _env_float,
    _env_int,
    _env_truthy,
    _extract_json,
    _is_retryable_codex_failure,
    _load_question,
    _sandbox_for,
)
from unsloth_bridge import (  # noqa: E402
    BridgeConfig,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    UnslothResponsesBridge,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)
from clin_genomic_analysis_benchmark.agent.contract import (  # noqa: E402
    ANALYZE_RESULT_SCHEMA,
    validate_result,
)


PROVIDER_NAME = "local_unsloth_qwen3_6_35b_a3b"
PROVIDER_DISPLAY_NAME = "Local Unsloth Studio (Qwen 3.6 35B-A3B)"
DEFAULT_MODEL = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"
DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"
MODEL_CONTEXT_WINDOW = 262_144
TOKEN_ENV = "UNSLOTH_STUDIO_AUTH_TOKEN"
FALLBACK_TOKEN_ENV = "API_TOKEN"
BRIDGE_TOKEN_ENV = "UNSLOTH_STUDIO_BRIDGE_KEY"


def _build_unsloth_prompt(question: dict) -> str:
    prompt = _build_prompt(question)
    return (
        f"{prompt}\n\n"
        "## Local-model analysis completion guard\n"
        "Finish the computation in this invocation. Work toward one complete "
        "analysis script instead of repeatedly exploring or rewriting partial "
        "scripts. Inspect only the files and columns needed for the requested "
        "estimand. If a command or script fails, fix and rerun it immediately. "
        "Never end a turn by announcing what you will inspect, build, fix, or "
        "run next: perform that action with a tool in the same turn. Do not "
        "stop after planning a final script. Once you have a defensible result, "
        "stop further refinement and return the required contract JSON as your "
        "final message. A best contract-valid result is preferable to an "
        "unfinished exhaustive analysis.\n"
    )


def _build_analyze_continuation_prompt(
    question: dict, previous_text: str, continuation: int
) -> str:
    previous = previous_text.strip()
    if len(previous) > 2_000:
        previous = previous[-2_000:]
    return (
        f"# Same-model analysis completion continuation {continuation}\n\n"
        "You are continuing an unfinished cancer-data benchmark analysis in the "
        "same writable scratch directory. The previous invocation already received "
        "the full benchmark instructions and created scripts here. Do not restart "
        "the analysis, reread the general instructions, or broadly explore the "
        "cohort.\n\n"
        f"Question: {question['question_text']}\n\n"
        f"Output contract: {question['instructions']}\n\n"
        "Start by inspecting the existing .py files in the current directory. "
        "Select the most complete script, run it, and fix its concrete errors. Use "
        "at most 8 shell commands. Do not inspect raw data unless a script error "
        "cannot be repaired from its traceback and code. Never end by announcing "
        "a future check, fix, script, or command: execute it now. As soon as the "
        "script yields a defensible result, return only the contract JSON with no "
        "Markdown or prose.\n\n"
        "The previous invocation's final message was:\n"
        f"{previous or '(empty)'}\n"
    )


def _normalize_analyze_result(obj: dict | None) -> dict | None:
    """Map common model field aliases onto the benchmark's typed contract."""
    if not isinstance(obj, dict):
        return obj
    normalized = dict(obj)
    answer_type_aliases = {
        "hazard_ratio": "hazard_ratio_with_ci",
        "odds_ratio": "odds_ratio_with_ci",
        "median": "median_with_ci",
        "p_value": "pvalue",
    }
    answer_type = normalized.get("answer_type")
    if isinstance(answer_type, str):
        answer_type = answer_type_aliases.get(answer_type, answer_type)
        normalized["answer_type"] = answer_type
    answer = normalized.get("answer")
    if not isinstance(answer, dict) or not isinstance(answer_type, str):
        return normalized
    answer = dict(answer)

    aliases_by_type: dict[str, dict[str, tuple[str, ...]]] = {
        "count": {"value": ("count",)},
        "proportion": {"value": ("proportion", "rate")},
        "median_with_ci": {
            "value": ("median", "median_months"),
            "ci_low": (
                "ci_lower",
                "ci_lower_months",
                "lower_ci",
                "ci_95_low",
            ),
            "ci_high": (
                "ci_upper",
                "ci_upper_months",
                "upper_ci",
                "ci_95_high",
            ),
            "n_total": ("n_patients",),
        },
        "hazard_ratio_with_ci": {
            "value": ("hazard_ratio", "hr"),
            "ci_low": ("ci_lower", "hr_ci_lower", "lower_ci", "ci_95_low"),
            "ci_high": ("ci_upper", "hr_ci_upper", "upper_ci", "ci_95_high"),
            "n_total": ("n_patients",),
        },
        "odds_ratio_with_ci": {
            "value": ("odds_ratio", "or"),
            "ci_low": ("ci_lower", "or_ci_lower", "lower_ci", "ci_95_low"),
            "ci_high": ("ci_upper", "or_ci_upper", "upper_ci", "ci_95_high"),
            "n_total": ("n_patients",),
        },
        "pvalue": {
            "value": ("p_value", "pvalue"),
            "test_name": ("test",),
            "n_total": ("n_patients",),
        },
        "categorical": {"value": ("category",)},
    }
    for canonical, aliases in aliases_by_type.get(answer_type, {}).items():
        if canonical in answer:
            continue
        for alias in aliases:
            if alias in answer:
                answer[canonical] = answer[alias]
                break
    normalized["answer"] = answer
    return normalized


def _model() -> str:
    model = (
        os.environ.get("UNSLOTH_MODEL", "").strip()
        or os.environ.get("CODEX_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    if any(character.isspace() for character in model):
        raise RuntimeError("UNSLOTH_MODEL must be a single model identifier")
    return model


def _base_url() -> str:
    """Return a validated Studio API root, adding ``/v1`` if absent."""
    raw = os.environ.get("UNSLOTH_STUDIO_BASE_URL", DEFAULT_BASE_URL).strip()
    raw = raw.rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "UNSLOTH_STUDIO_BASE_URL must be an absolute HTTP(S) URL"
        )
    if parsed.username or parsed.password:
        raise RuntimeError(
            f"put Studio credentials in {TOKEN_ENV}, not UNSLOTH_STUDIO_BASE_URL"
        )
    if parsed.query or parsed.fragment:
        raise RuntimeError(
            "UNSLOTH_STUDIO_BASE_URL must not contain a query or fragment"
        )
    if not parsed.path.rstrip("/").endswith("/v1"):
        raw += "/v1"
    return raw


def _studio_token() -> str:
    token = (
        os.environ.get(TOKEN_ENV, "").strip()
        or os.environ.get(FALLBACK_TOKEN_ENV, "").strip()
    )
    if not token:
        raise RuntimeError(
            f"Unsloth Studio requires authentication; export {TOKEN_ENV} "
            "with a Studio API key"
        )
    return token


def _provider_override(key: str, value: str) -> list[str]:
    return ["-c", f"model_providers.{PROVIDER_NAME}.{key}={json.dumps(value)}"]


def _bridge_config() -> BridgeConfig:
    return BridgeConfig(
        base_url=_base_url(),
        model=_model(),
        api_key=_studio_token(),
        request_timeout_seconds=_env_float(
            "UNSLOTH_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            minimum=1.0,
        ),
        max_retries=_env_int("UNSLOTH_MAX_RETRIES", 3),
        retry_base_seconds=_env_float(
            "UNSLOTH_RETRY_BASE_SECONDS", 2.0, minimum=0.0
        ),
        max_retry_sleep_seconds=_env_float(
            "UNSLOTH_MAX_RETRY_SLEEP_SECONDS", 30.0, minimum=0.0
        ),
        max_requests=_env_int("UNSLOTH_MAX_REQUESTS", 256),
    )


def _save_attempt_logs(
    *,
    scratch_dir: Path,
    stage: str,
    attempt: int,
    proc: subprocess.CompletedProcess[str],
    call_label: str = "",
) -> None:
    if not _env_truthy("CODEX_SAVE_ATTEMPT_LOGS", True):
        return
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    suffix = f".{call_label}" if call_label else ""
    (audit_dir / f"codex_unsloth_attempt.{stage}.{attempt}{suffix}.stdout.txt").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"codex_unsloth_attempt.{stage}.{attempt}{suffix}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _is_retryable_unsloth_failure(stderr: str) -> bool:
    low = stderr.lower()
    return _is_retryable_codex_failure(stderr) or any(
        term in low
        for term in (
            "connection refused",
            "connection timed out",
            "error sending request",
            "502 bad gateway",
            "504 gateway timeout",
        )
    )


def _codex_call(
    *,
    prompt: str,
    question: dict,
    last_message_file: Path,
    call_label: str = "",
) -> str:
    stage = TASK
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("CODEX_MAX_ATTEMPTS", 1)
    retry_sleep = _env_float("CODEX_RETRY_BASE_SECONDS", 15.0)
    config = _bridge_config()
    output_schema_path = scratch_dir / f".codex_output_schema.{stage}.json"
    output_schema_path.write_text(json.dumps(ANALYZE_RESULT_SCHEMA, indent=2))

    codex_bin = os.environ.get("CODEX_BIN", "codex")
    cmd = [
        codex_bin,
        "exec",
        "-C",
        str(SANDBOX_SCRATCH_DIR),
        # The cohort is readable without --add-dir. Codex treats added dirs as
        # writable roots, and since 0.162 its sandbox mkdirs protected paths
        # (e.g. .aws) inside them, which fails on the read-only cohort mount.
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
    cmd += [
        "--output-schema",
        str(SANDBOX_SCRATCH_DIR / output_schema_path.name),
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
            / (
                f"unsloth_bridge.{stage}.{attempt}"
                f"{f'.{call_label}' if call_label else ''}.jsonl"
            )
        )
        with UnslothResponsesBridge(config, audit_path=audit_path) as bridge:
            attempt_cmd = [*cmd]
            attempt_cmd += _provider_override("name", PROVIDER_DISPLAY_NAME)
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
            # The Studio credential remains in this trusted adapter process and
            # never reaches Codex or model-launched shell commands.
            env.pop(TOKEN_ENV, None)
            env.pop(FALLBACK_TOKEN_ENV, None)
            # Qwen thinking mode is managed by Studio, not Codex's proprietary
            # reasoning-effort request field.
            env.pop("CODEX_REASONING_EFFORT", None)

            with sandboxed_agent_command(
                attempt_cmd,
                cohort_dir=cohort_dir,
                data_dictionary_path=question["data_dictionary_path"],
                scratch_dir=scratch_dir,
                environment=env,
                home_kind="codex_qwen",
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
                        / f"codex_unsloth_attempt_{stage}_{attempt}"
                    ),
                    home_kind="codex_qwen",
                )

        _save_attempt_logs(
            scratch_dir=scratch_dir,
            stage=stage,
            attempt=attempt,
            proc=proc,
            call_label=call_label,
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
        if attempt >= max_attempts or not _is_retryable_unsloth_failure(stderr):
            break
        sleep_for = retry_sleep * attempt
        sys.stderr.write(
            f"adapter: Codex attempt {attempt}/{max_attempts} failed retryably; "
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
    stage = TASK
    scratch_dir = Path(question["scratch_dir"])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    last_message_file = scratch_dir / f".codex_last_message.{stage}.txt"
    text = ""
    obj: dict | None = None
    analyze_continuations = _env_int("CODEX_ANALYZE_CONTINUATIONS", 2)
    for continuation in range(analyze_continuations + 1):
        call_label = f"continuation_{continuation}" if continuation else ""
        current_last_message = (
            scratch_dir / f".codex_last_message.{stage}.{call_label}.txt"
            if call_label
            else last_message_file
        )
        try:
            prompt = (
                _build_unsloth_prompt(question)
                if continuation == 0
                else _build_analyze_continuation_prompt(
                    question, text, continuation
                )
            )
            text = _codex_call(
                prompt=prompt,
                question=question,
                last_message_file=current_last_message,
                call_label=call_label,
            )
        except Exception as exc:
            sys.stderr.write(f"adapter: Codex/Unsloth invocation failed: {exc}\n")
            return 3
        obj = _normalize_analyze_result(_extract_json(text))
        result_errors = validate_result(obj) if obj is not None else []
        if obj is not None and not result_errors:
            break
        if continuation < analyze_continuations:
            sys.stderr.write(
                "adapter: analyze final was not valid contract JSON; continuing "
                "the same model from existing scratch work\n"
            )

    if obj is None or validate_result(obj):
        sys.stderr.write("adapter: could not extract JSON from Codex output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
