"""Narrow, auditable integrity adjudications for completed benchmark runs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import RUNS_DIR
from ..utils.jsonio import atomic_write_json
from . import isolation


CURRENT_RUN_MOUNT_PATH_POLICY = "current_run_mount_source_path_only_v1"
ADJUDICATED_FOR_SCORING = "adjudicated_for_scoring"


class IntegrityAdjudicationError(ValueError):
    """The run does not satisfy the selected narrow exception policy."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_run_dir(run_path: str | Path) -> Path:
    candidate = Path(run_path).expanduser()
    run_dir = candidate.resolve() if candidate.is_absolute() else (RUNS_DIR / candidate).resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run dir does not exist: {run_dir}")
    if not run_dir.is_relative_to(RUNS_DIR.resolve()):
        raise IntegrityAdjudicationError(
            f"run must be located under the configured runs root: {RUNS_DIR.resolve()}"
        )
    return run_dir


def _finding_key(finding: dict[str, str]) -> tuple[str, str]:
    return str(finding.get("path") or ""), str(finding.get("marker") or "")


def adjudicate_current_run_mount_path_leak(
    *,
    run_path: str | Path,
    reviewer: str,
    rationale: str,
) -> tuple[Path, dict[str, Any]]:
    """Accept only a procfs leak of this run's own scratch mount path for scoring.

    This is intentionally not a general quarantine override. It requires an
    Antigravity schema-v1 run whose complete postflight finding set is confined
    to one archived agent session, whose only marker is the configured runs
    root, and whose every occurrence points into the current run. Evidence of
    a gold marker, a prior run, a symlink/special file, or any other session is
    rejected.
    """
    reviewer = reviewer.strip()
    rationale = rationale.strip()
    if not reviewer:
        raise IntegrityAdjudicationError("reviewer must not be empty")
    if not rationale:
        raise IntegrityAdjudicationError("rationale must not be empty")

    run_dir = _resolve_run_dir(run_path)
    manifest_path = run_dir / "manifest.json"
    runs_path = run_dir / "runs.json"
    manifest_bytes = manifest_path.read_bytes()
    runs_bytes = runs_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    integrity = manifest.get("integrity") or {}
    if integrity.get("status") != "quarantined":
        raise IntegrityAdjudicationError(
            "current-run mount-path adjudication requires integrity.status=quarantined"
        )
    if integrity.get("adapter") != "antigravity_gemini":
        raise IntegrityAdjudicationError(
            "current-run mount-path adjudication is limited to antigravity_gemini"
        )
    if str((integrity.get("sandbox") or {}).get("schema_version")) != "1":
        raise IntegrityAdjudicationError(
            "current-run mount-path adjudication is limited to sandbox schema version 1"
        )

    recorded = list((integrity.get("postflight") or {}).get(
        "forbidden_marker_findings"
    ) or [])
    if not recorded:
        raise IntegrityAdjudicationError("quarantined run has no recorded findings")
    per_question = run_dir / "per_question"
    fresh = isolation.audit_agent_artifacts(per_question, max_findings=10_000)
    if sorted(map(_finding_key, fresh)) != sorted(map(_finding_key, recorded)):
        raise IntegrityAdjudicationError(
            "current artifact audit does not match the manifest's quarantine findings"
        )

    runs_marker = str(RUNS_DIR.resolve())
    current_prefix = str(run_dir).encode()
    sessions: set[tuple[str, str, str, str]] = set()
    truncated_current_prefix_occurrences = 0
    for finding in fresh:
        path_text, marker = _finding_key(finding)
        if marker != runs_marker:
            raise IntegrityAdjudicationError(
                f"finding is not the current runs-root marker: {finding}"
            )
        parts = Path(path_text).parts
        if (
            len(parts) < 5
            or parts[2] != "adapter_audit"
            or not parts[3].startswith("agy.")
            or not parts[3].endswith(".session")
        ):
            raise IntegrityAdjudicationError(
                f"finding is outside an archived Antigravity session: {path_text}"
            )
        sessions.add((parts[0], parts[1], parts[2], parts[3]))
        artifact = per_question / path_text
        data = artifact.read_bytes()
        marker_bytes = runs_marker.encode()
        cursor = 0
        occurrences = 0
        while True:
            index = data.find(marker_bytes, cursor)
            if index < 0:
                break
            occurrences += 1
            if not data.startswith(current_prefix, index):
                # SQLite WAL pages can end mid-value. Accept only a NUL-ended
                # near-complete prefix of this exact run path; a different run
                # name, another printable path byte, or an earlier truncation
                # remains disqualifying.
                available = data[index:index + len(current_prefix)]
                common = 0
                for expected, actual in zip(current_prefix, available):
                    if expected != actual:
                        break
                    common += 1
                next_byte = data[index + common:index + common + 1]
                if common < len(current_prefix) - 16 or next_byte != b"\x00":
                    raise IntegrityAdjudicationError(
                        "runs-root occurrence does not point into the current run: "
                        f"{path_text}"
                    )
                truncated_current_prefix_occurrences += 1
            cursor = index + len(marker_bytes)
        if occurrences == 0:
            raise IntegrityAdjudicationError(
                f"recorded marker is no longer present in artifact: {path_text}"
            )

    if len(sessions) != 1:
        raise IntegrityAdjudicationError(
            "exception requires findings confined to exactly one archived agent session"
        )
    session = next(iter(sessions))
    session_root = per_question.joinpath(*session)
    transcript_data = b"\n".join(
        path.read_bytes()
        for path in sorted(session_root.rglob("transcript*.jsonl"))
        if path.is_file()
    )
    if b"/proc/1/task/1/mountinfo" not in transcript_data or b"/work rw" not in transcript_data:
        raise IntegrityAdjudicationError(
            "archived transcript does not establish the procfs scratch-mount leak"
        )
    if manifest.get("run_id", "").encode() not in transcript_data:
        raise IntegrityAdjudicationError(
            "archived transcript does not identify the current run"
        )

    record: dict[str, Any] = {
        "decision": "accepted_for_scoring",
        "policy": CURRENT_RUN_MOUNT_PATH_POLICY,
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reviewer": reviewer,
        "rationale": rationale,
        "prior_status": "quarantined",
        "manifest_sha256_before_adjudication": _sha256(manifest_bytes),
        "runs_sha256": _sha256(runs_bytes),
        "validated_scope": {
            "finding_count": len(fresh),
            "finding_sessions": ["/".join(session)],
            "only_current_run_path_occurrences": True,
            "truncated_current_run_path_occurrences": (
                truncated_current_prefix_occurrences
            ),
            "gold_marker_findings": 0,
            "prior_run_path_findings": 0,
            "original_postflight_preserved": True,
        },
    }
    integrity["status"] = ADJUDICATED_FOR_SCORING
    integrity["adjudication"] = record
    manifest["integrity"] = integrity
    atomic_write_json(manifest_path, manifest)
    return run_dir, record
