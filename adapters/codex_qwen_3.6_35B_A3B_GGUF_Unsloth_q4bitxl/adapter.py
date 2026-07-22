"""Codex + Unsloth Studio adapter for clin-genomic-analysis-benchmark.

Agent/harness combo: the OpenAI **Codex CLI** driving a local **Unsloth Studio**
server that serves `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` on port 8888. This mirrors
the user's interactive `codex --profile unsloth_api` setup, but also passes
provider overrides so the adapter is explicit about base_url, wire_api, auth env
key, and model.

For each stage this adapter:
  - reads the question.json passed by the harness,
  - builds a prompt = repo-root AGENT_INSTRUCTIONS.md + the stage payload,
  - runs `codex exec --profile unsloth_api` non-interactively with the scratch
    dir as the working root and a stage-appropriate sandbox,
  - reads Codex's final message (via `--output-last-message`),
  - pulls a JSON object out of it (tolerates Markdown fences / truncation),
  - writes a contract-compliant result.json.

Environment (all optional; sensible defaults):
  - UNSLOTH_STUDIO_AUTH_TOKEN
                          auth token for the endpoint
  - API_TOKEN             fallback auth token, copied to UNSLOTH_STUDIO_AUTH_TOKEN
                          when the latter is unset (default "EMPTY")
  - CODEX_PROFILE         Codex profile to use (default "unsloth_api")
  - CODEX_MODEL           override the model
                          (default "unsloth/Qwen3.6-35B-A3B-MTP-GGUF")
  - UNSLOTH_STUDIO_BASE_URL
                          endpoint base URL (default "http://127.0.0.1:8888/v1")
  - CODEX_BIN             path to the codex binary (default "codex" on PATH)
  - CODEX_SANDBOX_MODE    sandbox for the analyze stage (default "workspace-write";
                          set "danger-full-access" if the model can't read the
                          cohort under workspace-write on your platform)

This adapter is intentionally thin — the harness is what ensures correctness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
DEFAULT_CODEX_PROFILE = "unsloth_api"
DEFAULT_CODEX_PROVIDER = "unsloth_api"
DEFAULT_CODEX_MODEL = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"
DEFAULT_UNSLOTH_BASE_URL = "http://127.0.0.1:8888/v1"
UNSLOTH_TOKEN_ENV = "UNSLOTH_STUDIO_AUTH_TOKEN"


def _load_question(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _build_prompt(question: dict) -> str:
    """Prompt = the canonical AGENT_INSTRUCTIONS.md followed by the stage payload.

    Codex `exec` has no `--append-system-prompt`; we prepend the instructions to
    the user prompt so the model gets the same guidance the Claude adapter serves
    as a system prompt. Single source of truth: repo-root AGENT_INSTRUCTIONS.md.
    """
    repo_root = ADAPTER_DIR.parent.parent
    instructions_doc = (repo_root / "AGENT_INSTRUCTIONS.md").read_text()
    stage = question["stage"]
    payload = (
        f"# Question\n"
        f"- ID: {question['question_id']}\n"
        f"- Cohort: {question['cohort']}\n"
        f"- Category: {question['category']}\n"
        f"- Stage: {stage}\n\n"
        f"## Cohort directory (READ-ONLY)\n"
        f"`{question['cohort_dir']}`\n\n"
        f"## Data dictionary\n"
        f"`{question['data_dictionary_path']}`\n\n"
        f"## Question text\n"
        f"> {question['question_text']}\n\n"
        f"## Harness instructions\n"
        f"{question['instructions']}\n\n"
        f"Your current working directory is your scratch directory (writable): "
        f"`{question['scratch_dir']}`. Write any intermediate files there. The "
        f"cohort directory is READ-ONLY — read from it, never write to it. Use the "
        f"shell to run Python (pandas/numpy/scipy/statsmodels/lifelines) for analysis.\n\n"
        f"Output ONLY the final JSON object as your very last message, with no "
        f"surrounding prose and no Markdown fences."
    )
    return f"{instructions_doc}\n\n---\n\n{payload}\n"


# ---------------------------------------------------------------------------
# JSON extraction (copied verbatim from adapters/claude_code/adapter.py — a
# battle-tested tolerant parser: whole-text → fenced → brace-balanced → repair).
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_FIRST_OBJ_RE = re.compile(r"(\{[\s\S]*\})", re.DOTALL)


def _balanced_object(candidate: str) -> str | None:
    """Walk `candidate` (must start with '{'), respecting string literals,
    and return the substring up to and including the matching closing brace.
    Returns None if no balanced object found.
    """
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(candidate):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return candidate[: i + 1]
    return None


def _repair_truncated_json(candidate: str) -> str | None:
    """Best-effort: candidate starts with '{' but is truncated.
    Close any open string, then close any open arrays/objects in LIFO order.
    Strips a dangling trailing comma / partial key=value fragment if present.
    Returns the repaired string, or None if not repairable.
    """
    in_string = False
    escape = False
    stack: list[str] = []  # stack of '{' or '['
    last_complete_value_end = -1  # index after the last comma or container open
    for i, ch in enumerate(candidate):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("{")
            last_complete_value_end = i + 1
        elif ch == "[":
            stack.append("[")
            last_complete_value_end = i + 1
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
                last_complete_value_end = i + 1
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
                last_complete_value_end = i + 1
        elif ch == "," and not in_string:
            last_complete_value_end = i + 1

    if not stack:
        return None  # already balanced — caller should have parsed it

    # Build the repaired string: close any open string and then unwind the stack.
    tail = ""
    if in_string:
        tail += '"'
    for opener in reversed(stack):
        tail += "}" if opener == "{" else "]"
    repaired = candidate + tail
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        pass

    # Trim back to the last completed key:value boundary (last comma or open brace)
    # then close. This drops the partial trailing field that broke the parse.
    if last_complete_value_end <= 0:
        return None
    head = candidate[:last_complete_value_end].rstrip()
    if head.endswith(","):
        head = head[:-1].rstrip()
    tail = ""
    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape = False
    for ch in head:
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1
    if in_string:
        return None
    tail = "]" * max(depth_arr, 0) + "}" * max(depth_obj, 0)
    repaired = head + tail
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        return None


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(text)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    m2 = _FIRST_OBJ_RE.search(text)
    if m2:
        candidate = m2.group(1)
        balanced = _balanced_object(candidate)
        if balanced is not None:
            try:
                v = json.loads(balanced)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            try:
                v = json.loads(repaired)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    if start >= 0:
        candidate = text[start:]
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            try:
                v = json.loads(repaired)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Codex invocation
# ---------------------------------------------------------------------------

def _sandbox_for(stage: str) -> str:
    """analyze needs to run code + write scratch; the read-only stages just
    inspect the data dictionary / file headers."""
    if stage == "analyze":
        return os.environ.get("CODEX_SANDBOX_MODE", "workspace-write")
    return "read-only"


def _codex_call(*, prompt: str, scratch_dir: str, sandbox_mode: str,
                last_message_file: Path) -> str:
    profile = os.environ.get("CODEX_PROFILE", DEFAULT_CODEX_PROFILE)
    provider = os.environ.get("CODEX_MODEL_PROVIDER", DEFAULT_CODEX_PROVIDER)
    base_url = os.environ.get("UNSLOTH_STUDIO_BASE_URL", DEFAULT_UNSLOTH_BASE_URL)
    provider_env_key = os.environ.get("CODEX_PROVIDER_ENV_KEY", UNSLOTH_TOKEN_ENV)
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    cmd = [
        codex_bin, "exec",
        "--profile", profile,
        "-C", scratch_dir,               # working root = writable scratch dir
        "--skip-git-repo-check",         # scratch dir is not a git repo
        "--sandbox", sandbox_mode,
        "-c", 'approval_policy="never"',  # fully non-interactive, no reviewer
        "-c", f"oss_provider={json.dumps(provider)}",
        "-c", f"model_provider={json.dumps(provider)}",
        "-c", f"model_providers.{provider}.name={json.dumps('Unsloth Studio')}",
        "-c", f"model_providers.{provider}.base_url={json.dumps(base_url)}",
        "-c", f"model_providers.{provider}.env_key={json.dumps(provider_env_key)}",
        "-c", f"model_providers.{provider}.wire_api={json.dumps('responses')}",
        "-c", f"model_providers.{provider}.requires_openai_auth=false",
        "--color", "never",
        "-o", str(last_message_file),    # final agent message → file
    ]
    model = os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    if model:
        cmd += ["-m", model]
    cmd += ["-"]                          # read the prompt from stdin

    env = os.environ.copy()
    token = env.get(UNSLOTH_TOKEN_ENV) or env.get("API_TOKEN") or "EMPTY"
    env[UNSLOTH_TOKEN_ENV] = token
    env.setdefault("API_TOKEN", token)

    proc = subprocess.run(cmd, input=prompt, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"codex exited {proc.returncode}: {proc.stderr[-1500:]}"
        )

    # Prefer the explicit last-message file; fall back to stdout if empty.
    text = ""
    if last_message_file.exists():
        text = last_message_file.read_text().strip()
    if not text:
        text = proc.stdout
    return text


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = _load_question(args.question_file)
    stage = question["stage"]
    scratch_dir = question["scratch_dir"]
    Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    last_message_file = Path(scratch_dir) / f".codex_last_message.{stage}.txt"

    prompt = _build_prompt(question)
    text = _codex_call(
        prompt=prompt,
        scratch_dir=scratch_dir,
        sandbox_mode=_sandbox_for(stage),
        last_message_file=last_message_file,
    )

    obj = _extract_json(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from codex output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
