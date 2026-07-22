"""Sandboxed execution for gold-standard analysis scripts.

Default sandbox: subprocess with
  - cohort_dir symlinked into a temp workdir as ./cohort/
  - env scrubbed (only PATH, HOME, LANG, PYTHONUNBUFFERED, LC_*, OPENBLAS/MKL nthreads)
  - RLIMIT_AS / RLIMIT_CPU caps via preexec_fn
  - network blocked via `unshare -n` if available (Linux)
  - timeout enforced via subprocess.run

The script is invoked as:  python <Q<id>.py> <abs cohort_dir> <result_json_path>
The script must write a JSON object to result_json_path matching the answer-type schema.
"""

from __future__ import annotations

import logging
import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resource limits (per-process)
_RLIMIT_CPU_S = 300                 # 5 minutes wall is the timeout; CPU also bounded
_RLIMIT_AS_BYTES = 8 * 1024**3      # 8 GB virtual memory cap


def _set_rlimits() -> None:
    """preexec_fn to apply rlimits in the child process."""
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (_RLIMIT_CPU_S, _RLIMIT_CPU_S + 5))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES))
    except (ValueError, OSError):
        pass


def _have_unshare() -> bool:
    return shutil.which("unshare") is not None


@dataclass
class SandboxResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_seconds: float
    result_path: Optional[Path] = None


def run(
    *,
    script_path: Path,
    cohort_dir: Path,
    result_path: Path,
    timeout_s: int = 300,
    block_network: bool = True,
    extra_env: Optional[dict[str, str]] = None,
) -> SandboxResult:
    """Run a gold-standard analysis script in a sandbox."""
    import time
    work = Path(tempfile.mkdtemp(prefix="clingen_gold_"))
    try:
        # Symlink cohort dir as ./cohort
        local_cohort = work / "cohort"
        local_cohort.symlink_to(cohort_dir.resolve(), target_is_directory=True)

        # Clean env
        keep = {"PATH", "HOME", "LANG", "PYTHONUNBUFFERED", "LC_ALL", "LC_CTYPE",
                "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"}
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.setdefault("OMP_NUM_THREADS", "2")
        env.setdefault("OPENBLAS_NUM_THREADS", "2")
        env.setdefault("MKL_NUM_THREADS", "2")
        env.setdefault("PYTHONUNBUFFERED", "1")
        if extra_env:
            env.update(extra_env)

        cmd: list[str] = []
        if block_network and _have_unshare():
            # `unshare -n` puts us in a new (empty) network namespace
            cmd = ["unshare", "-rn", "--"]
        # Use the same Python that's running this process (so pandas/lifelines/etc. are present)
        import sys
        cmd += [sys.executable, str(script_path.resolve()),
                str(local_cohort.resolve()), str(result_path.resolve())]

        logger.info("Running gold script: %s", " ".join(cmd))

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                preexec_fn=_set_rlimits,
            )
            duration = time.monotonic() - start
            return SandboxResult(
                success=(proc.returncode == 0),
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
                timed_out=False,
                duration_seconds=duration,
                result_path=result_path if result_path.exists() else None,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.monotonic() - start
            return SandboxResult(
                success=False,
                stdout=e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) + f"\n[TIMEOUT after {timeout_s}s]",
                exit_code=-1,
                timed_out=True,
                duration_seconds=duration,
                result_path=None,
            )
    finally:
        # Best-effort cleanup; symlink to cohort_dir means we won't delete data
        try:
            shutil.rmtree(work)
        except OSError:
            pass
