"""Retry score-relevant technical failures without mutating the source run.

The repair workflow copies a completed run, executes only failed stages that
could change its deterministic score, merges successful retries, records an
audit trail in the copied manifest, and regenerates the scorecard.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..cohorts import get_cohort
from ..config import RUNS_DIR, TimeoutConfig, settings
from ..questions import io as q_io
from ..questions.schema import PublicQuestion
from ..scoring.driver import score_run
from ..utils.jsonio import atomic_write_json
from .orchestrator import (
    QuestionRun,
    _SAFE_AGENT_ENV_VARS,
    _agent_provenance,
    _run_one_question,
)
from .isolation import (
    audit_agent_artifacts,
    require_supported_adapter,
    run_isolation_preflight,
    sandbox_backend_provenance,
)


@dataclass(frozen=True)
class RepairTarget:
    """One failed stage whose retry can affect the run's score."""

    question_id: str
    cohort: str
    stage: str
    original_failure_reason: str


@dataclass(frozen=True)
class RepairSummary:
    """Result returned after a repaired copy is written and rescored."""

    source_run_dir: Path
    repaired_run_dir: Path
    targets: tuple[RepairTarget, ...]
    successful_merges: int
    failed_retries: int


_STAGES = ("classify", "disambiguate", "analyze")
_CONFIG_ENV_VARS = set(_SAFE_AGENT_ENV_VARS) | {
    # These can override ANTHROPIC_VERTEX_PROJECT_ID in Claude Code.
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
}
_PROVENANCE_FIELDS = (
    "adapter",
    "provider",
    "model",
    "effort_level",
    "effort_supported",
    "project_id",
    "region",
)


def _resolve_run_dir(run_path: str | Path) -> Path:
    path = Path(run_path)
    resolved = path.resolve() if path.is_absolute() else (RUNS_DIR / path).resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"Run dir does not exist: {resolved}")
    for filename in ("manifest.json", "runs.json"):
        if not (resolved / filename).is_file():
            raise FileNotFoundError(f"Run is missing {filename}: {resolved}")
    return resolved


def _load_source(run_path: str | Path) -> tuple[Path, dict, list[dict]]:
    run_dir = _resolve_run_dir(run_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    runs = json.loads((run_dir / "runs.json").read_text())
    if not isinstance(manifest, dict) or not isinstance(runs, list):
        raise ValueError(f"Malformed run metadata in {run_dir}")
    return run_dir, manifest, runs


def _gold_classification(gold_question: Any) -> Optional[str]:
    if isinstance(gold_question, Mapping):
        value = gold_question.get("classification")
    else:
        value = getattr(gold_question, "classification", None)
    return str(value) if value is not None else None


def _has_gold_answer(gold_question: Any) -> bool:
    if isinstance(gold_question, Mapping):
        return gold_question.get("gold_answer") is not None
    return getattr(gold_question, "gold_answer", None) is not None


def _failure_reason(qrun: dict, stage: str) -> str:
    invocation = qrun.get(stage)
    if isinstance(invocation, dict) and invocation.get("failure_reason"):
        return str(invocation["failure_reason"])
    if qrun.get("error"):
        return str(qrun["error"])
    if invocation is None:
        return f"missing {stage} invocation"
    return f"unsuccessful {stage} invocation"


def _classification_label(qrun: dict) -> Optional[str]:
    invocation = qrun.get("classify")
    if not isinstance(invocation, dict) or not invocation.get("success"):
        return None
    label = (invocation.get("result") or {}).get("classification")
    return label if label in {"ambiguous", "unambiguous"} else None


def find_score_relevant_failures(
    runs: list[dict],
    gold_questions: Mapping[tuple[str, str], Any],
    *,
    configured_stages: set[str] | None = None,
) -> list[RepairTarget]:
    """Select technical failures that can change deterministic scoring.

    Downstream failures caused by an already-wrong classification are excluded:
    retrying them cannot recover points. A failed classification is retried as a
    full routed question because no downstream stage was originally available.
    """
    stages = set(_STAGES) if configured_stages is None else configured_stages
    targets: list[RepairTarget] = []
    for qrun in runs:
        cohort = str(qrun.get("cohort") or "")
        question_id = str(qrun.get("question_id") or "")
        gold = gold_questions.get((cohort, question_id))
        if gold is None:
            raise ValueError(f"Gold question not found for {cohort}/{question_id}")

        label = _classification_label(qrun)
        if "classify" in stages and label is None:
            targets.append(RepairTarget(
                question_id=question_id,
                cohort=cohort,
                stage="classify",
                original_failure_reason=_failure_reason(qrun, "classify"),
            ))
            continue

        # Partial evals intentionally omit classification and persist the
        # harness-side route in classification_gold. This value is never sent
        # to the agent, but lets automatic post-run repair retry its failed
        # disambiguate/analyze stage without mistaking classify for a failure.
        if label is None and "classify" not in stages:
            routed_label = qrun.get("classification_gold")
            if routed_label in {"ambiguous", "unambiguous"}:
                label = routed_label

        gold_label = _gold_classification(gold)
        if label == "ambiguous" and gold_label == "ambiguous" and "disambiguate" in stages:
            invocation = qrun.get("disambiguate")
            if not isinstance(invocation, dict) or not invocation.get("success"):
                targets.append(RepairTarget(
                    question_id=question_id,
                    cohort=cohort,
                    stage="disambiguate",
                    original_failure_reason=_failure_reason(qrun, "disambiguate"),
                ))
        elif (label == "unambiguous" and gold_label == "unambiguous"
              and _has_gold_answer(gold) and "analyze" in stages):
            invocation = qrun.get("analyze")
            if not isinstance(invocation, dict) or not invocation.get("success"):
                targets.append(RepairTarget(
                    question_id=question_id,
                    cohort=cohort,
                    stage="analyze",
                    original_failure_reason=_failure_reason(qrun, "analyze"),
                ))
    return targets


def _load_gold_questions(manifest: dict) -> dict[tuple[str, str], Any]:
    questions: dict[tuple[str, str], Any] = {}
    for cohort in manifest.get("cohorts") or []:
        bank = q_io.load_gold(cohort)
        if bank is None:
            raise FileNotFoundError(f"No gold question bank found for cohort {cohort}")
        questions.update({(cohort, q.id): q for q in bank.questions})
    return questions


def plan_failed_run(run_path: str | Path) -> tuple[Path, list[RepairTarget]]:
    """Return the source directory and score-relevant repair targets."""
    source_dir, manifest, runs = _load_source(run_path)
    source_integrity = manifest.get("integrity") or {"status": "unaudited"}
    if source_integrity.get("status") == "quarantined":
        raise ValueError(
            "refusing to repair a quarantined run; rerun it from scratch in the "
            "hardened sandbox"
        )
    configured = set((manifest.get("settings") or {}).get("stages") or _STAGES)
    targets = find_score_relevant_failures(
        runs,
        _load_gold_questions(manifest),
        configured_stages=configured,
    )
    return source_dir, targets


def _restored_agent_environment(original: dict) -> dict[str, str]:
    """Overlay non-secret original model configuration on current credentials."""
    env = dict(os.environ)
    for name in _CONFIG_ENV_VARS:
        env.pop(name, None)
    for name, value in (original.get("environment") or {}).items():
        if name in _CONFIG_ENV_VARS and value is not None:
            env[name] = str(value)

    adapter = original.get("adapter")
    if adapter == "claude_code":
        if original.get("model"):
            env["CLINGEN_CLAUDE_MODEL"] = str(original["model"])
        provider = original.get("provider")
        if provider == "google_vertex_ai":
            env["CLAUDE_CODE_USE_VERTEX"] = "1"
            if original.get("project_id"):
                env["ANTHROPIC_VERTEX_PROJECT_ID"] = str(original["project_id"])
            if original.get("region"):
                env["CLOUD_ML_REGION"] = str(original["region"])
        elif provider == "anthropic_api":
            env["CLAUDE_CODE_USE_VERTEX"] = "0"
        if original.get("effort_supported") and original.get("effort_level"):
            env["CLINGEN_CLAUDE_EFFORT"] = str(original["effort_level"])
    elif str(adapter or "").startswith("codex_"):
        if original.get("model"):
            env["CODEX_MODEL"] = str(original["model"])
        if original.get("provider"):
            env["CODEX_MODEL_PROVIDER"] = str(original["provider"])
        if original.get("effort_level"):
            env["CODEX_REASONING_EFFORT"] = str(original["effort_level"])
    elif adapter == "antigravity_gemini":
        if original.get("model"):
            env["AGY_MODEL"] = str(original["model"])
        if original.get("effort_level"):
            env["AGY_EFFORT"] = str(original["effort_level"])
        if original.get("project_id"):
            env["AGY_GCP_PROJECT"] = str(original["project_id"])
        if original.get("region"):
            env["AGY_GCP_LOCATION"] = str(original["region"])
        if original.get("mode"):
            env["AGY_MODE"] = str(original["mode"])
        if original.get("agent_profile"):
            env["AGY_AGENT"] = str(original["agent_profile"])
    return env


def _provenance_mismatches(original: dict, retry: dict) -> dict[str, dict[str, Any]]:
    mismatches: dict[str, dict[str, Any]] = {}
    for field in _PROVENANCE_FIELDS:
        if field in original and original.get(field) != retry.get(field):
            mismatches[field] = {
                "original": original.get(field),
                "retry": retry.get(field),
            }
    return mismatches


def _timeout_config(manifest: dict) -> TimeoutConfig:
    raw = ((manifest.get("settings") or {}).get("timeouts") or {})
    defaults = settings().timeouts
    values: dict[str, int] = {}
    for stage in _STAGES:
        try:
            value = int(raw.get(stage, getattr(defaults, stage)))
        except (TypeError, ValueError):
            value = getattr(defaults, stage)
        values[stage] = value if value > 0 else getattr(defaults, stage)
    return TimeoutConfig(**values)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_reference(path: Path) -> str:
    try:
        return str(path.relative_to(RUNS_DIR.resolve()))
    except ValueError:
        return str(path)


def _new_run_id(source_run_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{source_run_id}-repaired-{timestamp}-{uuid.uuid4().hex[:6]}"


def _validate_output_run_id(output_run_id: str) -> None:
    if (not output_run_id or Path(output_run_id).name != output_run_id
            or output_run_id in {".", ".."}):
        raise ValueError("--output-run-id must be a single directory name")


def _public_questions_for_targets(
    targets: list[RepairTarget],
) -> dict[tuple[str, str], PublicQuestion]:
    questions: dict[tuple[str, str], PublicQuestion] = {}
    for cohort in sorted({target.cohort for target in targets}):
        bank = q_io.load_public(cohort)
        if bank is None:
            raise FileNotFoundError(f"No public question bank found for cohort {cohort}")
        questions.update({(cohort, q.id): q for q in bank.questions})
    missing = [
        f"{target.cohort}/{target.question_id}"
        for target in targets
        if (target.cohort, target.question_id) not in questions
    ]
    if missing:
        raise ValueError(f"Public question(s) not found: {', '.join(missing)}")
    return questions


def retry_failed_run(
    *,
    run_path: str | Path,
    output_run_id: str | None = None,
    agent_cmd: str | None = None,
    max_parallel: int = 4,
    agent_max_attempts: int = 3,
    agent_retry_base_seconds: float = 5.0,
    scoring_config_path: Optional[Path] = None,
) -> RepairSummary:
    """Create, repair, and rescore a copy of ``run_path``.

    The source directory is never written. Only a successful retry is merged;
    an exhausted retry remains available under ``repairs/`` for diagnosis while
    the copied ``runs.json`` retains the original failed invocation.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be >= 1")
    if agent_max_attempts < 1:
        raise ValueError("agent_max_attempts must be >= 1")
    if agent_retry_base_seconds < 0:
        raise ValueError("agent_retry_base_seconds must be >= 0")

    source_dir, manifest, runs = _load_source(run_path)
    source_integrity = manifest.get("integrity") or {"status": "unaudited"}
    if source_integrity.get("status") == "quarantined":
        raise ValueError(
            "refusing to repair a quarantined run; rerun it from scratch in the "
            "hardened sandbox"
        )
    configured_stages = set((manifest.get("settings") or {}).get("stages") or _STAGES)
    targets = find_score_relevant_failures(
        runs,
        _load_gold_questions(manifest),
        configured_stages=configured_stages,
    )
    if not targets:
        raise ValueError(f"No score-relevant technical failures found in {source_dir}")

    original_provenance = manifest.get("agent_provenance") or {}
    repair_env = _restored_agent_environment(original_provenance)
    effective_agent_cmd = agent_cmd or manifest.get("agent_cmd")
    if not effective_agent_cmd:
        raise ValueError("Run manifest has no agent_cmd; provide --agent explicitly")
    adapter_name = require_supported_adapter(effective_agent_cmd)
    sandbox = sandbox_backend_provenance()
    preflight = run_isolation_preflight()
    retry_provenance = _agent_provenance(effective_agent_cmd, environ=repair_env)
    mismatches = _provenance_mismatches(original_provenance, retry_provenance)
    if mismatches:
        details = ", ".join(
            f"{field}: {values['original']!r} -> {values['retry']!r}"
            for field, values in mismatches.items()
        )
        raise ValueError(f"Retry agent provenance does not match source run ({details})")

    repaired_id = output_run_id or _new_run_id(str(manifest.get("run_id") or source_dir.name))
    _validate_output_run_id(repaired_id)
    repaired_dir = source_dir.parent / repaired_id
    if repaired_dir.exists():
        raise FileExistsError(f"Repaired run destination already exists: {repaired_dir}")

    public_questions = _public_questions_for_targets(targets)
    timeout_config = _timeout_config(manifest)
    repair_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_hashes = {
        "manifest.json": _file_sha256(source_dir / "manifest.json"),
        "runs.json": _file_sha256(source_dir / "runs.json"),
    }

    # A scorecard is gold data.  Preserve the source run, but never place its
    # scorecard in the repair tree before model-controlled retries execute.
    # copytree still refuses an existing destination, avoiding accidental merges.
    shutil.copytree(
        source_dir,
        repaired_dir,
        ignore=lambda _directory, names: [
            name for name in names if name in {"scorecard.json", "scorecard.md"}
        ],
    )
    attempt_root = repaired_dir / "repairs" / repair_id

    def execute(target: RepairTarget) -> tuple[RepairTarget, QuestionRun]:
        question = public_questions[(target.cohort, target.question_id)]
        if target.stage == "classify":
            stages = [stage for stage in _STAGES if stage in configured_stages]
            route = None
        else:
            stages = [target.stage]
            route = "ambiguous" if target.stage == "disambiguate" else "unambiguous"
        replacement = _run_one_question(
            agent_cmd=effective_agent_cmd,
            q=question,
            cohort=get_cohort(target.cohort),
            run_dir=attempt_root,
            stages=stages,
            agent_max_attempts=agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            gold_classification=route,
            agent_env=repair_env,
            timeout_config=timeout_config,
        )
        return target, replacement

    outcomes: list[tuple[RepairTarget, QuestionRun]] = []
    if max_parallel == 1:
        outcomes = [execute(target) for target in targets]
    else:
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = [pool.submit(execute, target) for target in targets]
            for future in as_completed(futures):
                outcomes.append(future.result())

    runs_by_key = {(q["cohort"], q["question_id"]): q for q in runs}
    audit_outcomes: list[dict[str, Any]] = []
    successful_merges = 0
    retry_findings: list[dict[str, str]] = []
    for target, replacement in sorted(
        outcomes, key=lambda item: (item[0].cohort, item[0].question_id, item[0].stage)
    ):
        original = runs_by_key[(target.cohort, target.question_id)]
        replacement_raw = asdict(replacement)
        question_attempt_dir = (
            attempt_root / "per_question" / target.cohort / target.question_id
        )
        target_findings = audit_agent_artifacts(question_attempt_dir)
        retry_findings.extend({
            **finding,
            "question_id": target.question_id,
            "cohort": target.cohort,
            "stage": target.stage,
        } for finding in target_findings)
        if target.stage == "classify":
            invocation = replacement_raw.get("classify") or {}
            merged = bool(invocation.get("success")) and not target_findings
            if merged:
                for stage in _STAGES:
                    original[stage] = replacement_raw.get(stage)
                original["error"] = replacement_raw.get("error")
        else:
            invocation = replacement_raw.get(target.stage) or {}
            merged = bool(invocation.get("success")) and not target_findings
            if merged:
                original[target.stage] = replacement_raw[target.stage]
                if original.get("error"):
                    original["error"] = None
        successful_merges += int(merged)
        audit_outcomes.append({
            **asdict(target),
            "retry_success": bool(invocation.get("success")),
            "merged": merged,
            "retry_failure_reason": invocation.get("failure_reason"),
            "attempt_count": invocation.get("attempt_count", 1),
            "integrity_findings": target_findings,
        })

    atomic_write_json(repaired_dir / "runs.json", runs)
    ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repair_record = {
        "repair_id": repair_id,
        "source_run": _run_reference(source_dir),
        "source_run_id": manifest.get("run_id"),
        "source_hashes": source_hashes,
        "started_at": started_at,
        "ended_at": ended_at,
        "agent_cmd": effective_agent_cmd,
        "agent_provenance": retry_provenance,
        "provenance_mismatches": mismatches,
        "selection_policy": "score_relevant_technical_failures_v1",
        "retry_settings": {
            "max_attempts": agent_max_attempts,
            "base_delay_seconds": agent_retry_base_seconds,
            "backoff": "exponential",
            "max_parallel": max_parallel,
            "timeouts": asdict(timeout_config),
        },
        "targets": audit_outcomes,
        "successful_merges": successful_merges,
        "failed_retries": len(targets) - successful_merges,
        "artifacts": str(attempt_root.relative_to(repaired_dir)),
    }
    manifest["run_id"] = repaired_id
    manifest["ended_at"] = ended_at
    manifest["n_completed"] = sum(1 for qrun in runs if not qrun.get("error"))
    manifest["derived_from_run"] = _run_reference(source_dir)
    manifest.setdefault("repair_history", []).append(repair_record)
    source_status = str(source_integrity.get("status") or "unaudited")
    if retry_findings:
        integrity_status = "quarantined"
    elif source_status == "valid":
        integrity_status = "valid"
    else:
        # Sandboxing the retries cannot retroactively certify successful stages
        # inherited from an older, unisolated source run.
        integrity_status = "unaudited"
    manifest["integrity"] = {
        "status": integrity_status,
        "adapter": adapter_name,
        "sandbox": sandbox,
        "preflight": preflight,
        "postflight": {
            "passed": not retry_findings,
            "forbidden_marker_findings": retry_findings,
        },
        "source_run_status": source_status,
    }
    atomic_write_json(repaired_dir / "manifest.json", manifest)

    score_run(
        run_path=str(repaired_dir),
        scoring_config_path=scoring_config_path,
    )
    return RepairSummary(
        source_run_dir=source_dir,
        repaired_run_dir=repaired_dir,
        targets=tuple(targets),
        successful_merges=successful_merges,
        failed_retries=len(targets) - successful_merges,
    )
