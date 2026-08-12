"""Codex CLI adapter for Qwen 3.6 35B-A3B on local Unsloth Studio."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

ADAPTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = ADAPTER_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

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
from unsloth_bridge import (  # noqa: E402
    BridgeConfig,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    UnslothResponsesBridge,
    UnslothResponsesClient,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)
from clin_genomic_analysis_benchmark.agent.contract import validate_result  # noqa: E402


PROVIDER_NAME = "local_unsloth_qwen3_6_35b_a3b"
DEFAULT_MODEL = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"
DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"
MODEL_CONTEXT_WINDOW = 262_144
TOKEN_ENV = "UNSLOTH_STUDIO_AUTH_TOKEN"
FALLBACK_TOKEN_ENV = "API_TOKEN"
BRIDGE_TOKEN_ENV = "UNSLOTH_STUDIO_BRIDGE_KEY"


def _build_unsloth_prompt(question: dict) -> str:
    prompt = _build_prompt(question)
    stage = question["stage"]
    if stage not in {"classify", "disambiguate"}:
        return prompt
    shell_limit = 4 if stage == "classify" else 2
    return (
        f"{prompt}\n\n"
        "## Local-model completion guard\n"
        f"Complete this {stage} stage in at most 8 assistant turns and "
        f"{shell_limit} shell commands. Do not enumerate full tables, print full "
        "column lists, or dump large files. Inspect only the smallest evidence "
        "needed for the decision. Never end a turn by merely announcing another "
        "inspection or saying what you will do next: either execute the necessary "
        "tool now or return the required JSON. If further inspection would only "
        "refine confidence, stop and return your best contract-compliant answer "
        "from the evidence already available.\n"
    )


def _stage_output_schema(question: dict) -> dict | None:
    stage = question["stage"]
    if stage == "classify":
        return {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": ["ambiguous", "unambiguous"],
                },
                "rationale": {"type": "string", "minLength": 1},
            },
            "required": ["classification", "rationale"],
            "additionalProperties": False,
        }
    if stage == "disambiguate":
        concept_ids = [
            entry["id"]
            for entry in question.get("disambiguation_concept_menu") or []
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry["id"]
        ]
        item_schema: dict = {"type": "string"}
        if concept_ids:
            item_schema["enum"] = concept_ids
        return {
            "type": "object",
            "properties": {
                "concept_ids": {
                    "type": "array",
                    "items": item_schema,
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "required": ["concept_ids"],
            "additionalProperties": False,
        }
    return None


def _response_output_text(response: Mapping[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, Mapping):
                continue
            if content.get("type") != "output_text":
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part for part in parts if part).strip()


def _contract_fallback_prompt(question: dict, previous_text: str = "") -> str:
    menu = "\n".join(
        f"- {entry['id']}: {entry.get('label', '')} — {entry.get('description', '')}"
        for entry in question.get("disambiguation_concept_menu") or []
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )
    prior = ""
    if previous_text:
        prior = (
            "\nYour previous answer was not a valid contract object. Do not repeat it:\n"
            f"{previous_text[:1000]}\n"
        )
    return (
        "You are the contract-finalization step for a cancer-data benchmark. "
        "You have no tools and must decide from the public question, stage "
        "instructions, and concept menu below. Never announce a plan or request "
        "more data. Apply conventional defaults; mark ambiguity only for a material "
        "choice represented by the menu.\n\n"
        f"Stage: {question['stage']}\n"
        f"Question: {question['question_text']}\n"
        f"Stage instructions: {question['instructions']}\n\n"
        f"Concept menu:\n{menu}\n"
        f"{prior}\n"
        "Return only the required JSON object, with no Markdown or prose."
    )


def _direct_contract_result(question: dict) -> dict:
    """Recover a malformed read-only-stage final with the same local model."""
    schema = _stage_output_schema(question)
    if schema is None:
        raise RuntimeError("contract fallback is only available for read-only stages")
    config = _bridge_config()
    stage = question["stage"]
    audit_path = (
        Path(question["scratch_dir"]).resolve().parent
        / "adapter_audit"
        / f"unsloth_contract_fallback.{stage}.jsonl"
    )

    def audit(record: dict[str, Any]) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a") as handle:
            handle.write(json.dumps({"timestamp": time.time(), **record}) + "\n")

    client = UnslothResponsesClient(config, audit)
    previous_text = ""
    for semantic_attempt in range(1, 3):
        response = client.complete({
            "model": config.model,
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": _contract_fallback_prompt(question, previous_text),
                }],
            }],
            "tools": [],
            "max_output_tokens": 4_096,
            "reasoning": {"effort": "none"},
            "chat_template_kwargs": {"enable_thinking": False},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"{stage}_result",
                    "schema": schema,
                    "strict": True,
                }
            },
            "stream": False,
        })
        previous_text = _response_output_text(response)
        result = _extract_json(previous_text)
        schema_errors = validate_result(result, stage) if result is not None else []
        valid_result = result is not None and not schema_errors
        audit({
            "event": "contract_fallback_result",
            "semantic_attempt": semantic_attempt,
            "output_item_count": len(response.get("output") or []),
            "output_character_count": len(previous_text),
            "valid_contract_json": valid_result,
            "schema_error_count": len(schema_errors),
        })
        if valid_result:
            return result
    raise RuntimeError("same-model contract fallback did not return valid JSON")


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
) -> None:
    if not _env_truthy("CODEX_SAVE_ATTEMPT_LOGS", True):
        return
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / f"codex_unsloth_attempt.{stage}.{attempt}.stdout.txt").write_text(
        proc.stdout or ""
    )
    (audit_dir / f"codex_unsloth_attempt.{stage}.{attempt}.stderr.txt").write_text(
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


def _codex_call(*, prompt: str, question: dict, last_message_file: Path) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("CODEX_MAX_ATTEMPTS", 1)
    retry_sleep = _env_float("CODEX_RETRY_BASE_SECONDS", 15.0)
    config = _bridge_config()
    if stage in {"classify", "disambiguate"}:
        config = replace(
            config,
            max_retries=min(
                config.max_retries,
                _env_int("UNSLOTH_READ_ONLY_MAX_RETRIES", 1),
            ),
            max_requests=min(
                config.max_requests,
                _env_int("UNSLOTH_READ_ONLY_MAX_REQUESTS", 8),
            ),
            max_output_tokens=_env_int(
                "UNSLOTH_READ_ONLY_MAX_OUTPUT_TOKENS", 8_192
            ),
        )
    output_schema = _stage_output_schema(question)
    output_schema_path: Path | None = None
    if output_schema is not None:
        output_schema_path = scratch_dir / f".codex_output_schema.{stage}.json"
        output_schema_path.write_text(json.dumps(output_schema, indent=2))

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
    if output_schema_path is not None:
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
            / f"unsloth_bridge.{stage}.{attempt}.jsonl"
        )
        with UnslothResponsesBridge(config, audit_path=audit_path) as bridge:
            attempt_cmd = [*cmd]
            attempt_cmd += _provider_override(
                "name", "Local Unsloth Studio (Qwen 3.6 35B-A3B)"
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
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    last_message_file = scratch_dir / f".codex_last_message.{stage}.txt"
    text = ""
    try:
        text = _codex_call(
            prompt=_build_unsloth_prompt(question),
            question=question,
            last_message_file=last_message_file,
        )
    except Exception as exc:
        sys.stderr.write(f"adapter: Codex/Unsloth invocation failed: {exc}\n")
        if stage not in {"classify", "disambiguate"}:
            return 3

    obj = _extract_json(text)
    contract_errors = (
        validate_result(obj, stage)
        if obj is not None and stage in {"classify", "disambiguate"}
        else []
    )
    if stage in {"classify", "disambiguate"} and (
        obj is None or contract_errors
    ):
        sys.stderr.write(
            "adapter: Codex final was not valid contract JSON; trying same-model "
            "no-tools finalizer\n"
        )
        try:
            obj = _direct_contract_result(question)
        except Exception as exc:
            sys.stderr.write(f"adapter: contract finalizer failed: {exc}\n")
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from Codex output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
