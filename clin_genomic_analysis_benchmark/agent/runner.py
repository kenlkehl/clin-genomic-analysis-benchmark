"""Per-stage agent invocation as a subprocess.

The harness writes a `question.json`, invokes the agent's CLI, and reads back
`result.json`. Failures (non-zero exit, missing/invalid output, schema violation,
or timeout) are retried with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.jsonio import atomic_write_json
from .contract import Stage, validate_question, validate_result

logger = logging.getLogger(__name__)


@dataclass
class StageAttempt:
    attempt: int
    success: bool
    exit_code: int
    timed_out: bool
    duration_seconds: float
    failure_reason: Optional[str]
    schema_errors: list[str]


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
    attempt_count: int
    attempts: list[StageAttempt]


def invoke(
    *,
    agent_cmd: str,
    question_payload: dict,
    question_path: Path,
    result_path: Path,
    stderr_log_path: Path,
    timeout_s: int,
    max_attempts: int = 3,
    retry_base_seconds: float = 5.0,
    agent_env: Optional[Mapping[str, str]] = None,
) -> StageInvocation:
    """Run one stage of one question against the agent CLI, retrying failures."""
    stage = question_payload["stage"]
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds must be >= 0")
    # Validate the question payload we're about to send
    qerrs = validate_question(question_payload)
    if qerrs:
        # We constructed the payload, so this is a harness bug, not the agent's fault
        raise ValueError(f"harness bug: question.json fails schema: {qerrs}")

    question_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_json(question_path, question_payload)

    cmd_parts = shlex.split(agent_cmd) + [
        "--question-file", str(question_path),
        "--output", str(result_path),
    ]
    logger.info("Invoking agent: %s", " ".join(cmd_parts))

    overall_start = time.monotonic()
    attempts: list[StageAttempt] = []
    log_sections: list[str] = []
    exit_code = -1
    timed_out = False
    stdout = ""
    stderr = ""
    failure_reason: Optional[str] = None
    schema_errors: list[str] = []
    result: Optional[dict] = None

    for attempt_number in range(1, max_attempts + 1):
        # A failed adapter can leave partial or stale output. Every attempt must
        # earn its result independently.
        if result_path.exists():
            result_path.unlink()

        attempt_start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd_parts,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=dict(agent_env) if agent_env is not None else None,
            )
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            timed_out = True
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            ) + f"\n[TIMEOUT after {timeout_s}s]"
        attempt_duration = time.monotonic() - attempt_start

        failure_reason = None
        schema_errors = []
        result = None
        if timed_out:
            failure_reason = "timeout"
        elif exit_code != 0:
            failure_reason = f"exit_code={exit_code}"
        elif not result_path.exists():
            failure_reason = "agent did not produce result.json"
        else:
            try:
                loaded_result = json.loads(result_path.read_text())
            except json.JSONDecodeError as exc:
                failure_reason = f"result.json not valid JSON: {exc}"
            else:
                if not isinstance(loaded_result, dict):
                    failure_reason = "result.json is not a JSON object"
                else:
                    result = loaded_result
                    schema_errors = validate_result(result, stage)
                    if schema_errors:
                        failure_reason = "schema_violation"

        success = failure_reason is None
        attempts.append(StageAttempt(
            attempt=attempt_number,
            success=success,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=attempt_duration,
            failure_reason=failure_reason,
            schema_errors=schema_errors,
        ))
        log_sections.append(
            f"=== ATTEMPT {attempt_number}/{max_attempts} ===\n"
            f"Result: {'success' if success else failure_reason}\n\n"
            f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n"
        )
        if success:
            break
        if attempt_number < max_attempts:
            delay = retry_base_seconds * (2 ** (attempt_number - 1))
            logger.warning(
                "Agent %s stage %s attempt %d/%d failed (%s); retrying in %.1fs",
                question_payload.get("question_id"), stage, attempt_number,
                max_attempts, failure_reason, delay,
            )
            if delay:
                time.sleep(delay)

    duration = time.monotonic() - overall_start
    stderr_log_path.write_text("\n".join(log_sections))
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
        attempt_count=len(attempts),
        attempts=attempts,
    )
