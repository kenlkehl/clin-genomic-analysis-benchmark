"""Regression tests for narrowly scoped quarantine adjudication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clin_genomic_analysis_benchmark.agent import integrity, isolation


def _quarantined_run(tmp_path: Path, monkeypatch, *, prior_run: bool = False) -> Path:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "antigravity" / "run-current"
    session = (
        run_dir / "per_question/panc_1.2/panc_1.2-Q1/adapter_audit"
        / "agy.disambiguate.1.session"
    )
    transcript = session / "brain/session/.system_generated/logs/transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    referenced = (
        runs_root / "antigravity" / "run-prior"
        if prior_run
        else run_dir / "per_question/panc_1.2/panc_1.2-Q1/scratch"
    )
    transcript.write_text(
        f'{{"content":"/proc/1/task/1/mountinfo {referenced} /work rw"}}\n'
    )
    if not prior_run:
        # WAL pages may preserve a NUL-ended path value that is truncated near
        # a page boundary; it is still uniquely a prefix of this current run.
        (session / "conversation.db-wal").write_bytes(
            str(run_dir).encode()[:-2] + b"\x00"
        )

    monkeypatch.setattr(integrity, "RUNS_DIR", runs_root)
    monkeypatch.setattr(isolation, "RUNS_DIR", runs_root)
    findings = isolation.audit_agent_artifacts(run_dir / "per_question")
    manifest = {
        "agent_name": "antigravity",
        "run_id": "run-current",
        "cohorts": ["panc_1.2"],
        "integrity": {
            "status": "quarantined",
            "adapter": "antigravity_gemini",
            "sandbox": {"schema_version": "1"},
            "postflight": {
                "passed": False,
                "forbidden_marker_findings": findings,
            },
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "runs.json").write_text("[]")
    return run_dir


def test_adjudicates_only_current_run_procfs_mount_path(tmp_path, monkeypatch):
    run_dir = _quarantined_run(tmp_path, monkeypatch)

    resolved, record = integrity.adjudicate_current_run_mount_path_leak(
        run_path=run_dir,
        reviewer="test-reviewer",
        rationale="Only the current scratch bind source was disclosed; no content was exposed.",
    )

    assert resolved == run_dir.resolve()
    assert record["policy"] == integrity.CURRENT_RUN_MOUNT_PATH_POLICY
    assert record["validated_scope"]["gold_marker_findings"] == 0
    assert record["validated_scope"]["truncated_current_run_path_occurrences"] == 1
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["integrity"]["status"] == "adjudicated_for_scoring"
    assert manifest["integrity"]["postflight"]["passed"] is False
    assert manifest["integrity"]["adjudication"]["prior_status"] == "quarantined"


def test_adjudication_rejects_prior_run_path(tmp_path, monkeypatch):
    run_dir = _quarantined_run(tmp_path, monkeypatch, prior_run=True)

    with pytest.raises(
        integrity.IntegrityAdjudicationError,
        match="does not point into the current run",
    ):
        integrity.adjudicate_current_run_mount_path_leak(
            run_path=run_dir,
            reviewer="test-reviewer",
            rationale="should be rejected",
        )
