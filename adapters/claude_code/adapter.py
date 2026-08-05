"""Reference Claude Code adapter for clin-genomic-analysis-benchmark.

For each stage, this adapter:
  - reads the question.json passed by the harness,
  - constructs a stage-specific prompt,
  - shells out to `claude --print --output-format json` with the cohort directory
    bound via --add-dir and a narrow tool allow-list,
  - extracts the final assistant text,
  - pulls a JSON object out of it (tolerates Markdown fences),
  - writes a contract-compliant result.json.

Environment expected:
  - ANTHROPIC_VERTEX_PROJECT_ID (we'll default to kehllab-caia-v2 if unset)
  - CLAUDE_CODE_USE_VERTEX=1
  - CLINGEN_CLAUDE_EFFORT (optional; explicitly passes Claude's effort level)
  - claude CLI on PATH

This adapter is intentionally short — the harness is what ensures correctness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from clin_genomic_analysis_benchmark.agent.isolation import (
    SANDBOX_COHORT_DIR,
    SANDBOX_SCRATCH_DIR,
    export_agent_session_audit,
    sandbox_question_view,
    sandboxed_agent_command,
)

ADAPTER_DIR = Path(__file__).resolve().parent
_MAX_ERROR_STREAM_CHARS = 4000
_PROJECT_ID_PLACEHOLDERS = {
    "your_gcp_project_id",
    "your-gcp-project-id",
    "your-project-id",
}
_CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


class ClaudeInvocationError(RuntimeError):
    """Claude Code failed before returning a usable assistant response."""


def _read(path: Path) -> str:
    return path.read_text()


def _load_question(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _build_prompt(question: dict) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the requested stage."""
    question = sandbox_question_view(question)
    stage = question["stage"]
    # Single source of truth: the repo-root AGENT_INSTRUCTIONS.md (covers all three
    # stages, the current conventions, and the answer schemas). The user payload below
    # tells the agent which stage it is on.
    repo_root = ADAPTER_DIR.parent.parent
    sys_prompt = _read(repo_root / "AGENT_INSTRUCTIONS.md")
    user = (
        f"# Question\n"
        f"- ID: {question['question_id']}\n"
        f"- Cohort: {question['cohort']}\n"
        f"- Category: {question['category']}\n"
        f"- Stage: {stage}\n\n"
        f"## Cohort directory (read-only)\n"
        f"`{question['cohort_dir']}`\n\n"
        f"## Data dictionary\n"
        f"`{question['data_dictionary_path']}`\n\n"
        f"## Question text\n"
        f"> {question['question_text']}\n\n"
        f"## Harness instructions\n"
        f"{question['instructions']}\n\n"
        f"Use only Read, Glob, Grep, and Bash for analysis. The cohort directory "
        f"is bound; you may write to a scratch directory at: `{question['scratch_dir']}`. "
        f"Output ONLY the final JSON object as your last message, with no surrounding prose."
    )
    return sys_prompt, user


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
                # A string ended — could be a key or a value; treat as
                # candidate complete-value end (we'll trim after if dangling).
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
    # First, attempt the simple case: just close the open string (if any) and
    # then append the matching closers in LIFO order.
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
    # Recompute the still-open stack at this index by re-walking head.
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
    # Try whole text
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
    # Greedy {...} with brace balancing that respects string literals
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
        # Last resort: response was truncated mid-JSON. Try to repair.
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            try:
                v = json.loads(repaired)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                pass
    # Fall back to repairing from the first '{' even if no closing brace exists
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


def _allowed_tools_for(stage: str) -> str:
    """Allow agentic file-reading and Python execution; deny network/edit tools."""
    if stage == "analyze":
        # Analysis needs to run code
        return "Read,Glob,Grep,Bash"
    return "Read,Glob,Grep"


def _truncate_error_stream(text: str, limit: int = _MAX_ERROR_STREAM_CHARS) -> str:
    """Bound subprocess diagnostics while making truncation explicit."""
    text = text.strip()
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n...[truncated {omitted} characters]"


def _claude_failure_message(proc: subprocess.CompletedProcess[str]) -> str:
    """Preserve both CLI streams; Claude may emit structured errors on stdout."""
    parts = [f"claude exited {proc.returncode}"]
    for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        diagnostic = _truncate_error_stream(stream or "")
        if diagnostic:
            parts.append(f"--- {label} ---\n{diagnostic}")
    if len(parts) == 1:
        parts.append(
            "Claude Code produced no stdout or stderr; check its session/debug logs"
        )
    return "\n".join(parts)


def _claude_call(*, system_prompt: str, user_prompt: str, cohort_dir: str,
                 data_dictionary_path: str, scratch_dir: str,
                 allowed_tools: str) -> str:
    env = os.environ.copy()
    env.setdefault("ANTHROPIC_VERTEX_PROJECT_ID", "kehllab-caia-v2")
    env["CLAUDE_CODE_USE_VERTEX"] = env.get("CLAUDE_CODE_USE_VERTEX", "1")

    model = env.get("CLINGEN_CLAUDE_MODEL", "claude-opus-4-8")
    effort = env.get("CLINGEN_CLAUDE_EFFORT", "").strip().lower()
    if effort and effort not in _CLAUDE_EFFORT_LEVELS:
        allowed = ", ".join(sorted(_CLAUDE_EFFORT_LEVELS))
        raise ClaudeInvocationError(
            f"invalid CLINGEN_CLAUDE_EFFORT={effort!r}; choose one of: {allowed}"
        )
    if effort and "haiku-4-5" in model.lower():
        raise ClaudeInvocationError(
            f"CLINGEN_CLAUDE_EFFORT is set to {effort!r}, but {model!r} "
            "does not support configurable effort"
        )

    cmd = [
        env.get("CLAUDE_BIN", "claude"),
        "--print",
        "--output-format", "json",
        "--model", model,
        "--add-dir", str(SANDBOX_COHORT_DIR),
        "--add-dir", str(SANDBOX_SCRATCH_DIR),
        "--allowedTools", allowed_tools,
        "--append-system-prompt", system_prompt,
    ]
    if effort:
        cmd.extend(["--effort", effort])
    cmd.append(user_prompt)

    project_id = env.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
    if (env["CLAUDE_CODE_USE_VERTEX"] == "1"
            and project_id.lower() in _PROJECT_ID_PLACEHOLDERS):
        raise ClaudeInvocationError(
            "ANTHROPIC_VERTEX_PROJECT_ID is still a placeholder "
            f"({project_id!r}); set it to a real GCP project ID"
        )

    with sandboxed_agent_command(
        cmd,
        cohort_dir=cohort_dir,
        data_dictionary_path=data_dictionary_path,
        scratch_dir=scratch_dir,
        environment=env,
        home_kind="claude",
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
            home_kind="claude",
        )
    if proc.returncode != 0:
        raise ClaudeInvocationError(_claude_failure_message(proc))
    # `--output-format json` returns: {"type":"result","result":"<final assistant text>",...}
    try:
        outer = json.loads(proc.stdout)
        if isinstance(outer, dict):
            text = outer.get("result", "") or outer.get("response", "")
            if not text and "messages" in outer:
                # fall back to last assistant message
                for m in reversed(outer["messages"]):
                    if isinstance(m, dict) and m.get("role") == "assistant":
                        text = m.get("content", "")
                        if isinstance(text, list):
                            text = "".join(b.get("text", "") for b in text
                                           if isinstance(b, dict))
                        break
            return text
    except json.JSONDecodeError:
        pass
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
        # Keep the harness log concise and actionable rather than burying the
        # provider response in an uncaught Python traceback.
        sys.stderr.write(f"adapter: {exc}\n")
        return 4

    obj = _extract_json(text)
    if obj is None:
        # Fail loud — the harness will record this as failure_reason="result.json not valid JSON"
        sys.stderr.write("adapter: could not extract JSON from claude output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
