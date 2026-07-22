"""Per-stage agent invocation as a subprocess.

The harness writes a `question.json`, invokes the agent's CLI, and reads back
`result.json`. Failures (non-zero exit, schema violation, timeout) are recorded
but not retried by default.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.jsonio import atomic_write_json
from .contract import Stage, validate_question, validate_result

logger = logging.getLogger(__name__)


@dataclass
class StageInvocation:
    stage: Stage
    success: bool
    exit_code: int
    timed_out: bool
    duration_seconds: float
    result: Optional[dict]
    schema_errors: list[str]
    failure_reason: Optional[str]
    stdout: str
    stderr: str


def invoke(
    *,
    agent_cmd: str,
    question_payload: dict,
    question_path: Path,
    result_path: Path,
    stderr_log_path: Path,
    timeout_s: int,
) -> StageInvocation:
    """Run one stage of one question against the agent CLI."""
    stage = question_payload["stage"]
    # Validate the question payload we're about to send
    qerrs = validate_question(question_payload)
    if qerrs:
        # We constructed the payload, so this is a harness bug, not the agent's fault
        raise ValueError(f"harness bug: question.json fails schema: {qerrs}")

    question_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove any stale result file
    if result_path.exists():
        result_path.unlink()

    atomic_write_json(question_path, question_payload)

    cmd_parts = shlex.split(agent_cmd) + [
        "--question-file", str(question_path),
        "--output", str(result_path),
    ]
    logger.info("Invoking agent: %s", " ".join(cmd_parts))

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as e:
        exit_code = -1
        timed_out = True
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) \
                 + f"\n[TIMEOUT after {timeout_s}s]"
    duration = time.monotonic() - start

    # Persist stderr/stdout so users can debug
    if stderr or stdout:
        stderr_log_path.write_text(
            f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n"
        )

    failure_reason: Optional[str] = None
    schema_errors: list[str] = []
    result: Optional[dict] = None

    if timed_out:
        failure_reason = "timeout"
    elif exit_code != 0:
        failure_reason = f"exit_code={exit_code}"
    elif not result_path.exists():
        failure_reason = "agent did not produce result.json"
    else:
        try:
            result = json.loads(result_path.read_text())
        except json.JSONDecodeError as e:
            failure_reason = f"result.json not valid JSON: {e}"
        if result is not None:
            if not isinstance(result, dict):
                failure_reason = "result.json is not a JSON object"
                result = None
            else:
                schema_errors = validate_result(result, stage)
                if schema_errors:
                    failure_reason = "schema_violation"

    success = failure_reason is None
    return StageInvocation(
        stage=stage,
        success=success,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=duration,
        result=result,
        schema_errors=schema_errors,
        failure_reason=failure_reason,
        stdout=stdout,
        stderr=stderr,
    )
