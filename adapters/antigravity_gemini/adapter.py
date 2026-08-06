"""Google Antigravity CLI adapter for clin-genomic-analysis-benchmark.

This adapter invokes the Antigravity CLI (``agy``) in print mode with an
explicit ``AGY_MODEL`` and keeps the benchmark contract identical to the other
adapters:

    run.sh --question-file <question.json> --output <result.json>

The model-controlled process runs inside the benchmark's mandatory bubblewrap
boundary. It can see only the current cohort, dictionary, scratch directory,
ephemeral Antigravity home, and software runtime.
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
    AgentIsolationError,
    export_agent_session_audit,
    sandbox_question_view,
    sandboxed_agent_command,
)
from clin_genomic_analysis_benchmark.agent.contract import _RESULT_SCHEMAS

ADAPTER_DIR = Path(__file__).resolve().parent
_MAX_ERROR_STREAM_CHARS = 4000

DEFAULT_PRINT_TIMEOUTS = {
    "classify": "9m30s",
    "disambiguate": "4m30s",
    "analyze": "29m30s",
}


class AntigravityInvocationError(RuntimeError):
    """Antigravity failed before returning a usable assistant response."""


def _load_question(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _build_prompt(question: dict) -> str:
    question = sandbox_question_view(question)
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


def _agy_model() -> str:
    model = os.environ.get("AGY_MODEL", "").strip()
    if not model:
        raise AntigravityInvocationError(
            "AGY_MODEL is required; choose an exact name reported by `agy models`"
        )
    return model


def _agy_mode() -> str:
    mode = os.environ.get("AGY_MODE", "accept-edits").strip()
    if mode not in {"accept-edits", "plan"}:
        raise AntigravityInvocationError(
            "AGY_MODE must be either 'accept-edits' or 'plan'"
        )
    return mode


def _agy_effort() -> str | None:
    effort = os.environ.get("AGY_EFFORT", "").strip().lower()
    if not effort:
        return None
    if effort not in {"low", "medium", "high"}:
        raise AntigravityInvocationError(
            "AGY_EFFORT must be 'low', 'medium', or 'high'"
        )
    return effort


def _extract_cli_result(text: str) -> dict | None:
    """Unwrap Antigravity JSON mode while retaining a plain-text fallback."""
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return _extract_json(text)

    def unwrap(value: object) -> dict | None:
        if isinstance(value, str):
            return _extract_json(value)
        if not isinstance(value, dict):
            return None
        if any(key in value for key in ("classification", "concept_ids", "answer_type")):
            return value
        for key in (
            "structured_output",
            "response",
            "result",
            "output",
            "content",
            "text",
            "message",
        ):
            extracted = unwrap(value.get(key))
            if extracted is not None:
                return extracted
        return None

    return unwrap(envelope)


def _next_audit_stem(audit_dir: Path, stage: str) -> str:
    stem = f"agy.{stage}"
    index = 1
    while (audit_dir / f"{stem}.{index}.stdout.txt").exists():
        index += 1
    return f"{stem}.{index}"


def _archive_process_artifacts(
    *,
    scratch_dir: Path,
    model_scratch_dir: Path | None = None,
    stage: str,
    proc: subprocess.CompletedProcess[str],
) -> tuple[Path, str]:
    """Move model-visible CLI logs aside and preserve both process streams."""
    audit_dir = scratch_dir.parent / "adapter_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stem = _next_audit_stem(audit_dir, stage)
    (audit_dir / f"{stem}.stdout.txt").write_text(proc.stdout or "")
    (audit_dir / f"{stem}.stderr.txt").write_text(proc.stderr or "")

    cli_log = (model_scratch_dir or scratch_dir) / f"agy.{stage}.log"
    if cli_log.is_symlink():
        raise AgentIsolationError(f"Antigravity CLI log became a symlink: {cli_log}")
    if cli_log.is_file():
        cli_log.replace(audit_dir / f"{stem}.cli.log")
    return audit_dir, stem


def _failure_details(proc: subprocess.CompletedProcess[str]) -> str:
    streams = []
    for label, value in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if value:
            streams.append(
                f"--- {label} ---\n{value[-_MAX_ERROR_STREAM_CHARS:]}"
            )
    return "\n".join(streams) or "<no process output>"


def _agy_call(*, prompt: str, question: dict) -> str:
    stage = question["stage"]
    scratch_dir = Path(question["scratch_dir"]).resolve()
    cohort_dir = Path(question["cohort_dir"]).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        os.environ.get("AGY_BIN", "agy"),
        "--log-file", str(SANDBOX_SCRATCH_DIR / f"agy.{stage}.log"),
        "--print-timeout", _print_timeout(stage),
        "--add-dir", str(SANDBOX_COHORT_DIR),
        "--add-dir", str(SANDBOX_SCRATCH_DIR),
        "--mode", _agy_mode(),
        "--model", _agy_model(),
        "--output-format", "json",
        "--json-schema", json.dumps(_RESULT_SCHEMAS[stage], separators=(",", ":")),
        "--disable-slash-commands",
    ]
    effort = _agy_effort()
    if effort:
        cmd.extend(["--effort", effort])
    agent = os.environ.get("AGY_AGENT", "").strip()
    if agent:
        cmd.extend(["--agent", agent])
    if _env_truthy("AGY_USE_SANDBOX", False):
        cmd.append("--sandbox")
    if _env_truthy("AGY_SKIP_PERMISSIONS", False):
        cmd.append("--dangerously-skip-permissions")
    cmd.append("--print")
    cmd.append(prompt)

    source_env = os.environ.copy()
    source_env.setdefault("NO_COLOR", "1")
    # A benchmark invocation must not mutate the installed CLI mid-run.
    source_env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    dictionary_path = Path(question["data_dictionary_path"]).resolve()
    with sandboxed_agent_command(
        cmd,
        cohort_dir=cohort_dir,
        data_dictionary_path=dictionary_path,
        scratch_dir=scratch_dir,
        environment=source_env,
        home_kind="antigravity",
    ) as launch:
        proc = subprocess.run(
            launch.command,
            env=launch.environment,
            capture_output=True,
            text=True,
        )
        audit_dir, stem = _archive_process_artifacts(
            scratch_dir=scratch_dir,
            model_scratch_dir=getattr(launch, "host_staged_scratch", scratch_dir),
            stage=stage,
            proc=proc,
        )
        export_agent_session_audit(
            launch,
            destination=audit_dir / f"{stem}.session",
            home_kind="antigravity",
        )
    if proc.returncode != 0:
        raise AntigravityInvocationError(
            f"agy exited {proc.returncode}\n{_failure_details(proc)}"
        )
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

    obj = _extract_cli_result(text)
    if obj is None:
        sys.stderr.write("adapter: could not extract JSON from antigravity output\n")
        sys.stderr.write(text[:2000] + "\n")
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
