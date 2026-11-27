"""Claude Code adapter for Claude on the Databricks Unity AI Gateway.

Claude Code speaks the Anthropic Messages API, which the gateway serves at
``{host}/ai-gateway/anthropic``. Each invocation starts a localhost-only
pass-through (``databricks_bridge.py``) that holds the Databricks credentials;
Claude Code receives only an ephemeral bridge token. Prompting and JSON
extraction are shared with the reference ``claude_code`` adapter.

Environment:
  - DATABRICKS_CLAUDE_ENDPOINT (required): the full gateway endpoint name,
    e.g. ``ai_gateway_model_services_prod.<team>_<project>.<...>-claude-haiku-5-5``
  - CLINGEN_CLAUDE_MODEL (optional): the Claude ID Claude Code is told; by
    default the ``claude-*`` suffix of the endpoint name
  - CLINGEN_CLAUDE_EFFORT (optional): passed to Claude Code's ``--effort``
  - DATABRICKS_CONFIG_PROFILE (optional, default ``unity-gateway``)
  - DATABRICKS_CLAUDE_REQUEST_TIMEOUT_SECONDS, DATABRICKS_CLAUDE_MAX_REQUESTS
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
    _CLAUDE_EFFORT_LEVELS,
    ClaudeInvocationError,
    _allowed_tools,
    _build_prompt,
    _claude_failure_message,
    _extract_json,
    _load_question,
)
from adapters.claude_code_databricks.databricks_bridge import (  # noqa: E402
    BridgeConfig,
    DatabricksAnthropicBridge,
    canonical_model,
)
from clin_genomic_analysis_benchmark.agent.isolation import (  # noqa: E402
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandboxed_agent_command,
)

HOME_KIND = "claude_bridge"
# Claude Code routes background work (titles, command checks, subagents) to
# its Haiku/Sonnet/Opus aliases; point every alias at the one model under test.
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
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


def _env_number(name: str, default: float, *, minimum: float, integer: bool = False) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise ClaudeInvocationError(f"{name} must be a number") from exc
    if value < minimum:
        raise ClaudeInvocationError(f"{name} must be at least {minimum}")
    return value


def _bridge_config() -> BridgeConfig:
    endpoint = os.environ.get("DATABRICKS_CLAUDE_ENDPOINT", "").strip()
    if not endpoint or any(c.isspace() for c in endpoint):
        raise ClaudeInvocationError(
            "DATABRICKS_CLAUDE_ENDPOINT must name the gateway endpoint, e.g. "
            "ai_gateway_model_services_prod.<team>_<project>.<...>-claude-haiku-5-5")
    model = (os.environ.get("CLINGEN_CLAUDE_MODEL", "").strip()
             or canonical_model(endpoint))
    if not model:
        raise ClaudeInvocationError(
            "set CLINGEN_CLAUDE_MODEL: the endpoint name does not end in a Claude model ID")
    return BridgeConfig(
        endpoint=endpoint,
        model=model,
        request_timeout_seconds=_env_number(
            "DATABRICKS_CLAUDE_REQUEST_TIMEOUT_SECONDS", 900.0, minimum=1.0),
        max_requests=int(_env_number("DATABRICKS_CLAUDE_MAX_REQUESTS", 500,
                                     minimum=1, integer=True)),
    )


def _effort() -> str | None:
    effort = os.environ.get("CLINGEN_CLAUDE_EFFORT", "").strip().lower()
    if effort and effort not in _CLAUDE_EFFORT_LEVELS:
        raise ClaudeInvocationError(
            f"invalid CLINGEN_CLAUDE_EFFORT={effort!r}; choose one of: "
            + ", ".join(sorted(_CLAUDE_EFFORT_LEVELS)))
    return effort or None


def _claude_environment(model: str, bridge: DatabricksAnthropicBridge) -> dict[str, str]:
    env = os.environ.copy()
    for name in _STRIP_ENV:
        env.pop(name, None)
    env["ANTHROPIC_BASE_URL"] = bridge.base_url
    env["ANTHROPIC_AUTH_TOKEN"] = bridge.bearer_token
    for name in MODEL_ALIAS_ENV:
        env[name] = model
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _claude_call(*, system_prompt: str, user_prompt: str, cohort_dir: str,
                 data_dictionary_path: str, scratch_dir: str,
                 allowed_tools: str) -> str:
    config = _bridge_config()
    effort = _effort()
    audit_path = (Path(scratch_dir).resolve().parent / "adapter_audit"
                  / "databricks_bridge.analyze.jsonl")
    with DatabricksAnthropicBridge(config, audit_path=audit_path) as bridge:
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
        ]
        if effort:
            cmd.extend(["--effort", effort])
        cmd.append(user_prompt)
        with sandboxed_agent_command(
            cmd,
            cohort_dir=cohort_dir,
            data_dictionary_path=data_dictionary_path,
            scratch_dir=scratch_dir,
            environment=env,
            home_kind=HOME_KIND,
        ) as launch:
            proc = subprocess.run(launch.command, env=launch.environment,
                                  capture_output=True, text=True)
            export_agent_session_audit(
                launch,
                destination=Path(scratch_dir).resolve().parent / "agent_session_audit",
                home_kind=HOME_KIND,
            )
    if proc.returncode != 0:
        raise ClaudeInvocationError(_claude_failure_message(proc))
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if isinstance(outer, dict):
        if outer.get("is_error"):
            raise ClaudeInvocationError(
                f"claude reported an error result: {str(outer.get('result'))[:2000]}")
        return outer.get("result", "") or ""
    return proc.stdout


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = _load_question(args.question_file)
    system_prompt, user_prompt = _build_prompt(question)
    try:
        text = _claude_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            cohort_dir=question["cohort_dir"],
            data_dictionary_path=question["data_dictionary_path"],
            scratch_dir=question["scratch_dir"],
            allowed_tools=_allowed_tools(),
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
