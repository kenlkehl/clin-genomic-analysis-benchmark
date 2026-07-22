"""Codex CLI adapter using the user's default GPT-5.4 Azure config.

This adapter intentionally does not override model/provider settings. It relies
on ~/.codex/config.toml, which currently selects Azure + gpt-5.4. Before every
Codex invocation it refreshes AZURE_OPENAI_API_KEY with `az account
get-access-token` because Azure bearer tokens expire frequently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
AZURE_RESOURCE = "https://cognitiveservices.azure.com/"


def _load_question(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _build_prompt(question: dict) -> str:
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
        f"cohort directory is READ-ONLY -- read from it, never write to it. Use "
        f"Python (pandas/numpy/scipy/statsmodels/lifelines as needed) for "
        f"analysis stages.\n\n"
        f"Output ONLY the final JSON object as your very last message, with no "
        f"surrounding prose and no Markdown fences."
    )
    return f"{instructions_doc}\n\n---\n\n{payload}\n"


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_FIRST_OBJ_RE = re.compile(r"(\{[\s\S]*\})", re.DOTALL)


def _balanced_object(candidate: str) -> str | None:
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
    in_string = False
    escape = False
    stack: list[str] = []
    last_complete_value_end = -1
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
        return None

    tail = '"' if in_string else ""
    for opener in reversed(stack):
        tail += "}" if opener == "{" else "]"
    repaired = candidate + tail
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        pass

    if last_complete_value_end <= 0:
        return None
    head = candidate[:last_complete_value_end].rstrip()
    if head.endswith(","):
        head = head[:-1].rstrip()

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
    repaired = head + ("]" * max(depth_arr, 0)) + ("}" * max(depth_obj, 0))
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
        repaired = _repair_truncated_json(text[start:])
        if repaired is not None:
            try:
                v = json.loads(repaired)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    return None


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} must be an integer") from e
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} must be a number") from e
    return max(minimum, value)


def _sandbox_for(stage: str) -> str:
    stage_key = f"CODEX_SANDBOX_MODE_{stage.upper()}"
    if os.environ.get(stage_key):
        return os.environ[stage_key]
    if os.environ.get("CODEX_SANDBOX_MODE"):
        return os.environ["CODEX_SANDBOX_MODE"]
    return "workspace-write" if stage == "analyze" else "read-only"


def _refresh_azure_token(env: dict[str, str]) -> None:
    if not _env_truthy("CODEX_REFRESH_AZURE_TOKEN", True):
        return
    attempts = _env_int("CODEX_AZ_TOKEN_ATTEMPTS", 3)
    retry_sleep = _env_float("CODEX_AZ_TOKEN_RETRY_SLEEP_SECONDS", 5.0)
    cmd = [
        "az", "account", "get-access-token",
        f"--resource={AZURE_RESOURCE}",
        "--query", "accessToken",
        "--output", "tsv",
    ]
    last_error = ""
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode == 0:
            token = proc.stdout.strip()
            if token:
                env["AZURE_OPENAI_API_KEY"] = token
                return
            last_error = "az token refresh returned an empty token"
        else:
            last_error = f"az token refresh failed: {proc.stderr[-2000:]}"
        if attempt < attempts:
            time.sleep(retry_sleep)
    raise RuntimeError(last_error)


def _is_retryable_codex_failure(stderr: str) -> bool:
    low = stderr.lower()
    if "content_filter" in low:
        return False
    retry_terms = (
        "response.failed event received",
        "stream disconnected before completion",
        "reconnecting... 5/5",
        "timeout waiting for child process",
        "temporarily unavailable",
        "connection reset",
        "rate limit",
        "429",
        "503",
    )
    return any(term in low for term in retry_terms)


def _save_attempt_logs(
    *,
    scratch_dir: Path,
    stage: str,
    attempt: int,
    proc: subprocess.CompletedProcess[str],
) -> None:
    if not _env_truthy("CODEX_SAVE_ATTEMPT_LOGS", True):
        return
    (scratch_dir / f".codex_attempt.{stage}.{attempt}.stdout.txt").write_text(
        proc.stdout or ""
    )
    (scratch_dir / f".codex_attempt.{stage}.{attempt}.stderr.txt").write_text(
        proc.stderr or ""
    )


def _codex_call(*, prompt: str, question: dict, last_message_file: Path) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = _env_int("CODEX_MAX_ATTEMPTS", 3)
    retry_sleep = _env_float("CODEX_RETRY_BASE_SECONDS", 15.0)

    codex_bin = os.environ.get("CODEX_BIN", "codex")
    cmd = [
        codex_bin, "exec",
        "-C", str(scratch_dir),
        "--add-dir", str(cohort_dir),
        "--skip-git-repo-check",
        "--sandbox", _sandbox_for(stage),
        "-c", 'approval_policy="never"',
        "--color", "never",
        "-o", str(last_message_file),
    ]
    if _env_truthy("CODEX_EPHEMERAL", True):
        cmd.append("--ephemeral")
    if os.environ.get("CODEX_PROFILE"):
        cmd += ["--profile", os.environ["CODEX_PROFILE"]]
    if os.environ.get("CODEX_MODEL"):
        cmd += ["--model", os.environ["CODEX_MODEL"]]
    cmd.append("-")

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        env = os.environ.copy()
        _refresh_azure_token(env)
        if last_message_file.exists():
            last_message_file.unlink()

        proc = subprocess.run(
            cmd,
            input=prompt,
            env=env,
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
            text = ""
            if last_message_file.exists():
                text = last_message_file.read_text().strip()
            if not text:
                text = proc.stdout
            return text

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
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = _load_question(args.question_file)
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"])
    scratch_dir.mkdir(parents=True, exist_ok=True)
    last_message_file = scratch_dir / f".codex_last_message.{stage}.txt"

    prompt = _build_prompt(question)
    try:
        text = _codex_call(
            prompt=prompt,
            question=question,
            last_message_file=last_message_file,
        )
    except Exception as e:
        sys.stderr.write(f"adapter: codex invocation failed: {e}\n")
        return 3

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
