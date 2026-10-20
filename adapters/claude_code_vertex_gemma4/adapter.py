"""Claude Code adapter for Gemma 4 MaaS on Google Vertex Agent Platform.

This uses the same localhost Anthropic-to-Chat bridge as the
``claude_code_vertex_gemini`` adapter, pointed at a Gemma 4 MaaS model. Gemma
does not return thought signatures, and its model card says thinking control
is unsupported, so no reasoning effort is sent. Settings share the
``VERTEX_GEMMA_*`` names used by the ``codex_vertex_gemma4_26b`` adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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
from adapters.claude_code_vertex_gemini.adapter import (  # noqa: E402
    HOME_KIND,
    _claude_environment,
    _env_float,
    _env_int,
)
from adapters.claude_code_vertex_gemini.anthropic_bridge import (  # noqa: E402
    AnthropicVertexBridge,
)
from adapters.codex_vertex_gemma4_26b.vertex_bridge import BridgeConfig  # noqa: E402
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)

DEFAULT_MODEL = "google/gemma-4-26b-a4b-it-maas"
DEFAULT_LOCATION = "global"


def _model() -> str:
    model = os.environ.get("VERTEX_GEMMA_MODEL", "").strip() or DEFAULT_MODEL
    if any(character.isspace() for character in model):
        raise ClaudeInvocationError("VERTEX_GEMMA_MODEL must be a single model identifier")
    return model if "/" in model else f"google/{model}"


def _bridge_config() -> BridgeConfig:
    project_id = os.environ.get("VERTEX_GEMMA_PROJECT_ID", "").strip()
    if not project_id or "/" in project_id:
        raise ClaudeInvocationError(
            "VERTEX_GEMMA_PROJECT_ID must be the Google Cloud project with "
            "Vertex Agent Platform Gemma access"
        )
    location = os.environ.get("VERTEX_GEMMA_LOCATION", DEFAULT_LOCATION).strip()
    if not location or "/" in location:
        raise ClaudeInvocationError("VERTEX_GEMMA_LOCATION must be a location name")
    return BridgeConfig(
        project_id=project_id,
        location=location,
        model=_model(),
        request_timeout_seconds=_env_float(
            "VERTEX_GEMMA_REQUEST_TIMEOUT_SECONDS", 600.0, minimum=1.0
        ),
        max_retries=_env_int("VERTEX_GEMMA_MAX_RETRIES", 6),
        retry_base_seconds=_env_float("VERTEX_GEMMA_RETRY_BASE_SECONDS", 5.0),
        max_retry_sleep_seconds=_env_float("VERTEX_GEMMA_MAX_RETRY_SLEEP_SECONDS", 60.0),
        max_output_tokens=_env_int("VERTEX_GEMMA_MAX_OUTPUT_TOKENS", 16_384),
        max_requests=_env_int("VERTEX_GEMMA_MAX_REQUESTS", 256),
    )


def _claude_call(*, system_prompt: str, user_prompt: str, cohort_dir: str,
                 data_dictionary_path: str, scratch_dir: str,
                 allowed_tools: str, stage: str) -> str:
    config = _bridge_config()
    audit_path = (
        Path(scratch_dir).resolve().parent
        / "adapter_audit"
        / f"vertex_gemma_bridge.{stage}.jsonl"
    )
    with AnthropicVertexBridge(config, audit_path=audit_path, reasoning_effort=None) as bridge:
        env = _claude_environment(config.model, bridge)
        cmd = [
            env.get("CLAUDE_BIN", "claude"),
            "--print",
            "--output-format", "json",
            "--model", config.model,
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
            stage=stage,
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
