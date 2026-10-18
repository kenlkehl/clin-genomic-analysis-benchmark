"""Claude Code adapter for Gemma 4 models on a vLLM server's Anthropic API.

vLLM serves the Anthropic Messages API (``/v1/messages``) alongside its
OpenAI-compatible routes, so Claude Code talks to it directly through
``ANTHROPIC_BASE_URL``. Prompting and JSON extraction are shared with the
reference ``claude_code`` adapter; only the backend differs.

The endpoint must not require authentication: Claude Code runs inside the
model-controlled sandbox, so any upstream token would be visible to the model.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ADAPTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = ADAPTER_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.claude_code.adapter import (  # noqa: E402
    ClaudeInvocationError,
    _allowed_tools_for,
    _build_prompt,
    _claude_failure_message,
    _extract_json,
    _load_question,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)

HOME_KIND = "claude_vllm"
# vLLM ignores the key when it runs without --api-key; Claude Code requires one.
PLACEHOLDER_AUTH_TOKEN = "vllm-no-auth"
# Claude Code routes background work (titles, command-prefix checks, subagents)
# to its Haiku/Sonnet/Opus aliases; point every alias at the served model.
MODEL_ALIAS_ENV = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
# Host settings that would route Claude Code to another provider or account.
_STRIP_ENV = (
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_REGION",
    "CLOUD_ML_REGION",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLINGEN_CLAUDE_EFFORT",
)


def _model() -> str:
    model = os.environ.get("VLLM_MODEL", "").strip()
    if not model:
        raise ClaudeInvocationError("VLLM_MODEL must name the served model")
    if any(character.isspace() for character in model):
        raise ClaudeInvocationError("VLLM_MODEL must be a single model identifier")
    return model


def _anthropic_base_url() -> str:
    """Return the server root for ANTHROPIC_BASE_URL.

    ``VLLM_BASE_URL`` follows the OpenAI convention used by the other vLLM
    adapters and may end in ``/v1``; Claude Code appends ``/v1/messages``
    itself, so a trailing ``/v1`` is removed.
    """
    raw = os.environ.get("VLLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        raise ClaudeInvocationError("VLLM_BASE_URL must be set")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ClaudeInvocationError("VLLM_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ClaudeInvocationError("VLLM_BASE_URL must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ClaudeInvocationError("VLLM_BASE_URL must not contain a query or fragment")
    if raw.endswith("/v1"):
        raw = raw[: -len("/v1")]
    return raw


def _check_no_upstream_token() -> None:
    token = os.environ.get("VLLM_TOKEN", "").strip()
    if token and token.upper() != "EMPTY":
        raise ClaudeInvocationError(
            "VLLM_TOKEN is set, but this adapter supports only unauthenticated "
            "vLLM endpoints: Claude Code runs inside the model sandbox, so the "
            "token would be visible to the model"
        )


def _claude_environment(model: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in _STRIP_ENV:
        env.pop(name, None)
    env["ANTHROPIC_BASE_URL"] = _anthropic_base_url()
    env["ANTHROPIC_AUTH_TOKEN"] = PLACEHOLDER_AUTH_TOKEN
    for name in MODEL_ALIAS_ENV:
        env[name] = model
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _claude_call(*, system_prompt: str, user_prompt: str, cohort_dir: str,
                 data_dictionary_path: str, scratch_dir: str,
                 allowed_tools: str) -> str:
    _check_no_upstream_token()
    model = _model()
    env = _claude_environment(model)

    cmd = [
        env.get("CLAUDE_BIN", "claude"),
        "--print",
        "--output-format", "json",
        "--model", model,
        "--add-dir", str(SANDBOX_COHORT_DIR),
        "--add-dir", str(SANDBOX_SCRATCH_DIR),
        "--allowedTools", allowed_tools,
        "--append-system-prompt", system_prompt,
        user_prompt,
    ]

    with sandboxed_agent_command(
        cmd,
        cohort_dir=cohort_dir,
        data_dictionary_path=data_dictionary_path,
        scratch_dir=scratch_dir,
        environment=env,
        home_kind=HOME_KIND,
    ) as launch:
        proc = subprocess.run(
            launch.command,
            env=launch.environment,
            capture_output=True,
            text=True,
        )
        export_agent_session_audit(
            launch,
            destination=Path(scratch_dir).resolve().parent / "agent_session_audit",
            home_kind=HOME_KIND,
        )
    if proc.returncode != 0:
        raise ClaudeInvocationError(_claude_failure_message(proc))
    # `--output-format json` returns {"type": "result", "result": "<final text>", ...}
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if isinstance(outer, dict):
        if outer.get("is_error"):
            raise ClaudeInvocationError(
                f"claude reported an error result: {str(outer.get('result'))[:2000]}"
            )
        return outer.get("result", "") or ""
    return proc.stdout


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = _load_question(args.question_file)
    stage = question["stage"]
    system_prompt, user_prompt = _build_prompt(question)

    try:
        text = _claude_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            cohort_dir=question["cohort_dir"],
            data_dictionary_path=question["data_dictionary_path"],
            scratch_dir=question["scratch_dir"],
            allowed_tools=_allowed_tools_for(stage),
        )
    except ClaudeInvocationError as exc:
        sys.stderr.write(f"adapter: {exc}\n")
        return 4

    obj = _extract_json(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from claude output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
