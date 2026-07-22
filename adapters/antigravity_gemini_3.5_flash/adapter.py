"""Google Antigravity CLI adapter for clin-genomic-analysis-benchmark.

This adapter invokes the Antigravity CLI (`agy`) in print mode. The local
Antigravity settings select Gemini 3.5 Flash, and this wrapper keeps the
benchmark contract identical to the other adapters:

    run.sh --question-file <question.json> --output <result.json>

The adapter is intentionally thin: it builds a stage prompt from
AGENT_INSTRUCTIONS.md and the harness payload, lets Antigravity use its normal
CLI tools, extracts the final JSON object, and writes it to result.json.
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

DEFAULT_PRINT_TIMEOUTS = {
    "classify": "9m30s",
    "disambiguate": "4m30s",
    "analyze": "29m30s",
}


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
        f"Your current working directory is your scratch directory: "
        f"`{question['scratch_dir']}`. Write intermediate files only there. "
        f"The cohort directory is read-only. For analyze stages, use Python "
        f"(pandas/numpy/scipy/statsmodels/lifelines as needed) to compute the "
        f"answer from the cohort files.\n\n"
        f"Output ONLY the final JSON object as your last response, with no "
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


def _print_timeout(stage: str) -> str:
    stage_key = f"AGY_PRINT_TIMEOUT_{stage.upper()}"
    return (
        os.environ.get(stage_key)
        or os.environ.get("AGY_PRINT_TIMEOUT")
        or DEFAULT_PRINT_TIMEOUTS.get(stage, "5m")
    )


def _agy_call(*, prompt: str, question: dict) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    log_file = Path(os.environ.get("AGY_LOG_FILE", scratch_dir / f"agy.{stage}.log"))

    cmd = [
        os.environ.get("AGY_BIN", "agy"),
        "--log-file", str(log_file),
        "--print-timeout", _print_timeout(stage),
        "--add-dir", str(cohort_dir),
        "--add-dir", str(scratch_dir),
    ]
    if _env_truthy("AGY_USE_SANDBOX", True):
        cmd.append("--sandbox")
    if _env_truthy("AGY_SKIP_PERMISSIONS", True):
        cmd.append("--dangerously-skip-permissions")
    cmd.append("--print")
    cmd.append(prompt)

    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")

    proc = subprocess.run(
        cmd,
        cwd=str(scratch_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-2000:]
        raise RuntimeError(f"agy exited {proc.returncode}: {tail}")
    return proc.stdout


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = _load_question(args.question_file)
    prompt = _build_prompt(question)

    try:
        text = _agy_call(prompt=prompt, question=question)
    except Exception as e:
        sys.stderr.write(f"adapter: antigravity invocation failed: {e}\n")
        return 3

    obj = _extract_json(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from antigravity output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
