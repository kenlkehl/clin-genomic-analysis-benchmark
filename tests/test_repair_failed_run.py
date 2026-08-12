"""Tests for selective, non-destructive repair of failed benchmark stages."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from clin_genomic_analysis_benchmark.agent import repair
from clin_genomic_analysis_benchmark.agent.orchestrator import QuestionRun


def _invocation(*, success: bool, classification: str | None = None,
                reason: str | None = None) -> dict:
    result = {"classification": classification} if classification else None
    return {
        "success": success,
        "result": result,
        "failure_reason": reason,
        "attempt_count": 1,
    }


def test_find_score_relevant_failures_excludes_wrong_route_failures():
    runs = [
        {
            "cohort": "c",
            "question_id": "classify-failed",
            "classify": _invocation(success=False, reason="exit_code=3"),
        },
        {
            "cohort": "c",
            "question_id": "disambig-failed",
            "classify": _invocation(success=True, classification="ambiguous"),
            "disambiguate": _invocation(success=False, reason="timeout"),
        },
        {
            "cohort": "c",
            "question_id": "wrong-ambiguous-route",
            "classify": _invocation(success=True, classification="ambiguous"),
            "disambiguate": _invocation(success=False, reason="timeout"),
        },
        {
            "cohort": "c",
            "question_id": "analysis-failed",
            "classify": _invocation(success=True, classification="unambiguous"),
            "analyze": _invocation(success=False, reason="schema_violation"),
        },
        {
            "cohort": "c",
            "question_id": "wrong-unambiguous-route",
            "classify": _invocation(success=True, classification="unambiguous"),
            "analyze": _invocation(success=False, reason="schema_violation"),
        },
    ]
    gold = {
        ("c", "classify-failed"): {"classification": "ambiguous"},
        ("c", "disambig-failed"): {"classification": "ambiguous"},
        ("c", "wrong-ambiguous-route"): {"classification": "unambiguous"},
        ("c", "analysis-failed"): {
            "classification": "unambiguous",
            "gold_answer": {"value": 1},
        },
        ("c", "wrong-unambiguous-route"): {"classification": "ambiguous"},
    }

    targets = repair.find_score_relevant_failures(runs, gold)

    assert [(target.question_id, target.stage) for target in targets] == [
        ("classify-failed", "classify"),
        ("disambig-failed", "disambiguate"),
        ("analysis-failed", "analyze"),
    ]


def test_find_score_relevant_failures_respects_partial_run_stages():
    runs = [{
        "cohort": "c",
        "question_id": "q",
        "classify": None,
        "classification_gold": "ambiguous",
        "disambiguate": _invocation(success=False, reason="timeout"),
    }]
    gold = {("c", "q"): {"classification": "ambiguous"}}

    targets = repair.find_score_relevant_failures(
        runs,
        gold,
        configured_stages={"disambiguate"},
    )

    # A deliberately omitted classify stage is not mistaken for a failure;
    # its persisted harness-side route allows the intended stage to be retried.
    assert [(target.question_id, target.stage) for target in targets] == [
        ("q", "disambiguate")
    ]


def test_restored_environment_pins_original_claude_model_effort_and_project(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "current-wrong-project")
    monkeypatch.setenv("CLINGEN_CLAUDE_MODEL", "current-model")
    original = {
        "adapter": "claude_code",
        "provider": "google_vertex_ai",
        "model": "claude-sonnet-5",
        "effort_level": "xhigh",
        "effort_supported": True,
        "project_id": "original-project",
        "region": "global",
        "environment": {},
    }

    env = repair._restored_agent_environment(original)

    assert env["CLINGEN_CLAUDE_MODEL"] == "claude-sonnet-5"
    assert env["CLINGEN_CLAUDE_EFFORT"] == "xhigh"
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "original-project"
    assert env["CLOUD_ML_REGION"] == "global"
    assert "GOOGLE_CLOUD_PROJECT" not in env


def test_restored_environment_pins_original_antigravity_runtime(monkeypatch):
    monkeypatch.setenv("AGY_MODEL", "current-model")
    original = {
        "adapter": "antigravity_gemini",
        "provider": "google_antigravity",
        "model": "gemini-3.6-flash-high",
        "effort_level": "high",
        "project_id": "original-project",
        "region": "global",
        "mode": "accept-edits",
        "agent_profile": "benchmark-agent",
        "environment": {},
    }

    env = repair._restored_agent_environment(original)

    assert env["AGY_MODEL"] == "gemini-3.6-flash-high"
    assert env["AGY_EFFORT"] == "high"
    assert env["AGY_GCP_PROJECT"] == "original-project"
    assert env["AGY_GCP_LOCATION"] == "global"
    assert env["AGY_MODE"] == "accept-edits"
    assert env["AGY_AGENT"] == "benchmark-agent"


def test_retry_failed_run_copies_merges_a_success_and_preserves_source(
    tmp_path, monkeypatch
):
    source = tmp_path / "runs" / "agent" / "source-run"
    source.mkdir(parents=True)
    manifest = {
        "agent_cmd": "fake-agent",
        "agent_name": "agent",
        "run_id": "source-run",
        "cohorts": ["c"],
        "n_questions": 2,
        "n_completed": 2,
        "agent_provenance": {},
        "integrity": {"status": "valid"},
        "settings": {
            "stages": ["classify", "disambiguate", "analyze"],
            "timeouts": {"classify": 10, "disambiguate": 20, "analyze": 30},
        },
    }
    original_runs = [
        {
            "question_id": "q-failed",
            "cohort": "c",
            "category": 1,
            "classification_gold": "",
            "classify": _invocation(success=True, classification="ambiguous"),
            "disambiguate": _invocation(success=False, reason="timeout"),
            "analyze": None,
            "error": None,
        },
        {
            "question_id": "q-ok",
            "cohort": "c",
            "category": 1,
            "classification_gold": "",
            "classify": _invocation(success=True, classification="ambiguous"),
            "disambiguate": {
                "success": True,
                "result": {"concept_ids": ["OUTCOME_METRIC"]},
            },
            "analyze": None,
            "error": None,
        },
    ]
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "runs.json").write_text(json.dumps(original_runs))
    (source / "scorecard.json").write_text('{"gold":"must not be copied"}')
    (source / "scorecard.md").write_text("gold-bearing scorecard")
    marker = source / "per_question" / "c" / "q-failed" / "original.log"
    marker.parent.mkdir(parents=True)
    marker.write_text("original artifact")

    gold_questions = [
        SimpleNamespace(id="q-failed", classification="ambiguous", gold_answer=None),
        SimpleNamespace(id="q-ok", classification="ambiguous", gold_answer=None),
    ]
    public_questions = [
        SimpleNamespace(id="q-failed", category=1, text="failed question"),
        SimpleNamespace(id="q-ok", category=1, text="ok question"),
    ]
    monkeypatch.setattr(
        repair.q_io,
        "load_gold",
        lambda cohort: SimpleNamespace(questions=gold_questions),
    )
    monkeypatch.setattr(
        repair.q_io,
        "load_public",
        lambda cohort: SimpleNamespace(questions=public_questions),
    )
    monkeypatch.setattr(repair, "get_cohort", lambda cohort: SimpleNamespace(name=cohort))
    monkeypatch.setattr(repair, "require_supported_adapter", lambda command: "test")
    monkeypatch.setattr(repair, "sandbox_backend_provenance", lambda: {"backend": "test"})
    monkeypatch.setattr(repair, "run_isolation_preflight", lambda: {"passed": True})

    captured_kwargs = {}

    def fake_run_one_question(**kwargs):
        captured_kwargs.update(kwargs)
        return QuestionRun(
            question_id="q-failed",
            cohort="c",
            category=1,
            classification_gold="ambiguous",
            disambiguate={
                "success": True,
                "result": {"concept_ids": ["OUTCOME_METRIC"]},
                "failure_reason": None,
                "attempt_count": 2,
            },
        )

    monkeypatch.setattr(repair, "_run_one_question", fake_run_one_question)
    scored = []
    monkeypatch.setattr(
        repair,
        "score_run",
        lambda **kwargs: scored.append(kwargs) or kwargs["run_path"],
    )

    summary = repair.retry_failed_run(
        run_path=source,
        output_run_id="repaired-run",
        max_parallel=1,
        agent_max_attempts=3,
        agent_retry_base_seconds=0,
        timeout_overrides={"disambiguate": 90},
    )

    repaired = source.parent / "repaired-run"
    assert summary.repaired_run_dir == repaired
    assert summary.successful_merges == 1
    assert summary.failed_retries == 0
    assert captured_kwargs["stages"] == ["disambiguate"]
    assert captured_kwargs["gold_classification"] == "ambiguous"
    assert captured_kwargs["timeout_config"].classify == 10
    assert captured_kwargs["timeout_config"].disambiguate == 90
    assert captured_kwargs["timeout_config"].analyze == 30

    # Source result and artifacts remain untouched.
    assert json.loads((source / "runs.json").read_text()) == original_runs
    assert marker.read_text() == "original artifact"

    repaired_runs = json.loads((repaired / "runs.json").read_text())
    assert repaired_runs[0]["disambiguate"]["success"] is True
    assert repaired_runs[1] == original_runs[1]
    assert (repaired / "per_question" / "c" / "q-failed" / "original.log").exists()
    assert not (repaired / "scorecard.json").exists()
    assert not (repaired / "scorecard.md").exists()

    repaired_manifest = json.loads((repaired / "manifest.json").read_text())
    assert repaired_manifest["run_id"] == "repaired-run"
    assert repaired_manifest["derived_from_run"] == str(source)
    history = repaired_manifest["repair_history"][-1]
    assert history["selection_policy"] == "score_relevant_technical_failures_v1"
    assert history["successful_merges"] == 1
    assert history["retry_settings"]["timeouts"]["disambiguate"] == 90
    assert history["retry_settings"]["timeout_overrides"] == {
        "disambiguate": 90
    }
    assert history["targets"][0]["attempt_count"] == 2
    assert history["source_hashes"]["runs.json"]
    assert repaired_manifest["integrity"]["status"] == "valid"
    assert scored == [{"run_path": str(repaired), "scoring_config_path": None}]


def test_retry_failed_run_keeps_original_failure_when_retry_exhausts(
    tmp_path, monkeypatch
):
    source = tmp_path / "agent" / "source"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps({
        "agent_cmd": "fake-agent",
        "agent_name": "agent",
        "run_id": "source",
        "cohorts": ["c"],
        "agent_provenance": {},
        "settings": {"stages": ["classify", "disambiguate", "analyze"]},
    }))
    old_failure = _invocation(success=False, reason="timeout")
    (source / "runs.json").write_text(json.dumps([{
        "question_id": "q",
        "cohort": "c",
        "category": 1,
        "classify": _invocation(success=True, classification="ambiguous"),
        "disambiguate": old_failure,
        "error": None,
    }]))
    monkeypatch.setattr(
        repair.q_io,
        "load_gold",
        lambda cohort: SimpleNamespace(questions=[
            SimpleNamespace(id="q", classification="ambiguous", gold_answer=None)
        ]),
    )
    monkeypatch.setattr(
        repair.q_io,
        "load_public",
        lambda cohort: SimpleNamespace(questions=[
            SimpleNamespace(id="q", category=1, text="question")
        ]),
    )
    monkeypatch.setattr(repair, "get_cohort", lambda cohort: SimpleNamespace(name=cohort))
    monkeypatch.setattr(repair, "require_supported_adapter", lambda command: "test")
    monkeypatch.setattr(repair, "sandbox_backend_provenance", lambda: {"backend": "test"})
    monkeypatch.setattr(repair, "run_isolation_preflight", lambda: {"passed": True})
    monkeypatch.setattr(repair, "score_run", lambda **kwargs: kwargs["run_path"])
    monkeypatch.setattr(
        repair,
        "_run_one_question",
        lambda **kwargs: QuestionRun(
            question_id="q",
            cohort="c",
            category=1,
            classification_gold="ambiguous",
            disambiguate={
                "success": False,
                "result": None,
                "failure_reason": "timeout",
                "attempt_count": 3,
            },
        ),
    )

    summary = repair.retry_failed_run(
        run_path=source,
        output_run_id="repaired",
        max_parallel=1,
        agent_retry_base_seconds=0,
    )

    repaired_runs = json.loads((source.parent / "repaired" / "runs.json").read_text())
    assert repaired_runs[0]["disambiguate"] == old_failure
    assert summary.successful_merges == 0
    assert summary.failed_retries == 1


def test_plan_refuses_quarantined_source(tmp_path):
    source = tmp_path / "quarantined"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "agent_cmd": "bash adapters/claude_code/run.sh",
        "cohorts": [],
        "integrity": {"status": "quarantined"},
    }))
    (source / "runs.json").write_text("[]")

    try:
        repair.plan_failed_run(source)
    except ValueError as exc:
        assert "quarantined run" in str(exc)
    else:
        raise AssertionError("quarantined run should not be repairable")


def test_timeout_config_rejects_invalid_overrides():
    manifest = {"settings": {"timeouts": {
        "classify": 10,
        "disambiguate": 20,
        "analyze": 30,
    }}}

    configured = repair._timeout_config(
        manifest,
        {"classify": 100, "disambiguate": 200},
    )
    assert configured.classify == 100
    assert configured.disambiguate == 200
    assert configured.analyze == 30

    try:
        repair._timeout_config(manifest, {"unknown": 5})
    except ValueError as exc:
        assert "unknown timeout override" in str(exc)
    else:
        raise AssertionError("unknown stage should be rejected")


def test_repair_loop_continues_on_progress_until_complete(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"run_id": "source"}))
    (source / "runs.json").write_text("[]")
    target_one = repair.RepairTarget("q1", "c", "classify", "timeout")
    target_two = repair.RepairTarget("q2", "c", "disambiguate", "timeout")
    pass_one = source.parent / "loop-output"
    pass_two = source.parent / "loop-output-pass2"

    def fake_plan(path):
        path = path.resolve()
        if path == source.resolve():
            return path, [target_one]
        if path == pass_one.resolve():
            return path, [target_two]
        if path == pass_two.resolve():
            return path, []
        raise AssertionError(path)

    calls = []

    def fake_retry(**kwargs):
        calls.append(kwargs)
        repaired = pass_one if len(calls) == 1 else pass_two
        target = target_one if len(calls) == 1 else target_two
        source_for_pass = Path(kwargs["run_path"]).resolve()
        source_manifest = json.loads(
            (source_for_pass / "manifest.json").read_text()
        )
        history = list(source_manifest.get("repair_history") or [])
        history.append({
            "source_run": str(source_for_pass),
            "source_hashes": {
                "manifest.json": repair._file_sha256(
                    source_for_pass / "manifest.json"
                ),
                "runs.json": repair._file_sha256(source_for_pass / "runs.json"),
            },
        })
        repaired.mkdir()
        (repaired / "manifest.json").write_text(json.dumps({
            "run_id": repaired.name,
            "derived_from_run": str(source_for_pass),
            "repair_history": history,
        }))
        (repaired / "runs.json").write_text("[]")
        return repair.RepairSummary(
            source_run_dir=source_for_pass,
            repaired_run_dir=repaired.resolve(),
            targets=(target,),
            successful_merges=1,
            failed_retries=0,
        )

    monkeypatch.setattr(repair, "plan_failed_run", fake_plan)
    monkeypatch.setattr(repair, "retry_failed_run", fake_retry)
    scored = []
    monkeypatch.setattr(
        repair,
        "score_run",
        lambda **kwargs: scored.append(kwargs) or kwargs["run_path"],
    )

    result = repair.retry_failed_run_until_stable(
        run_path=source,
        output_run_id="loop-output",
        max_repair_passes=3,
        timeout_overrides={"disambiguate": 900},
    )

    assert result.stop_reason == "complete"
    archive = tmp_path / "pre_repair"
    archived_source = archive / source.name
    archived_pass_one = archive / pass_one.name
    assert result.source_run_dir == archived_source
    assert result.final_run_dir == pass_two.resolve()
    assert result.remaining_targets == ()
    assert len(result.passes) == 2
    assert result.passes[0].source_run_dir == archived_source
    assert result.passes[0].repaired_run_dir == archived_pass_one
    assert result.passes[1].source_run_dir == archived_pass_one
    assert result.passes[1].repaired_run_dir == pass_two.resolve()
    assert archived_source.is_dir()
    assert archived_pass_one.is_dir()
    assert not source.exists()
    assert not pass_one.exists()
    assert calls[0]["output_run_id"] == "loop-output"
    assert calls[1]["output_run_id"] == "loop-output-pass2"
    assert calls[1]["timeout_overrides"] == {"disambiguate": 900}
    assert scored == [{
        "run_path": str(pass_two.resolve()),
        "scoring_config_path": None,
    }]

    final_manifest = json.loads((pass_two / "manifest.json").read_text())
    assert final_manifest["derived_from_run"] == str(archived_pass_one)
    assert [entry["source_run"] for entry in final_manifest["repair_history"]] == [
        str(archived_source),
        str(archived_pass_one),
    ]
    assert final_manifest["repair_history"][-1]["source_hashes"][
        "manifest.json"
    ] == repair._file_sha256(archived_pass_one / "manifest.json")


def test_archive_pre_final_runs_includes_legacy_top_level_ancestors(tmp_path):
    agent_dir = tmp_path / "agent"
    raw = agent_dir / "raw"
    pass_one = agent_dir / "pass-one"
    source = agent_dir / "pass-two"
    final = agent_dir / "pass-three"

    def write_run(path, parent=None):
        path.mkdir(parents=True)
        history = []
        if parent is not None:
            parent_manifest = json.loads((parent / "manifest.json").read_text())
            history = list(parent_manifest.get("repair_history") or [])
            history.append({
                "source_run": str(parent),
                "source_hashes": {
                    "manifest.json": repair._file_sha256(parent / "manifest.json"),
                    "runs.json": repair._file_sha256(parent / "runs.json"),
                },
            })
        (path / "manifest.json").write_text(json.dumps({
            "run_id": path.name,
            "derived_from_run": str(parent) if parent is not None else None,
            "repair_history": history,
        }))
        (path / "runs.json").write_text("[]")

    write_run(raw)
    write_run(pass_one, raw)
    write_run(source, pass_one)
    write_run(final, source)
    summary = repair.RepairSummary(source, final, (), 1, 0)

    archived_source, summaries, final_dir = repair._archive_pre_final_runs(
        source,
        [summary],
    )

    archive = agent_dir / "pre_repair"
    assert archived_source == archive / source.name
    assert summaries[0].source_run_dir == archive / source.name
    assert summaries[0].repaired_run_dir == final
    assert final_dir == final
    assert final.is_dir()
    assert sorted(path.name for path in archive.iterdir()) == [
        "pass-one",
        "pass-two",
        "raw",
    ]
    assert sorted(path.name for path in agent_dir.iterdir()) == [
        "pass-three",
        "pre_repair",
    ]

    final_manifest = json.loads((final / "manifest.json").read_text())
    assert final_manifest["derived_from_run"] == str(archive / source.name)
    assert [record["source_run"] for record in final_manifest["repair_history"]] == [
        str(archive / raw.name),
        str(archive / pass_one.name),
        str(archive / source.name),
    ]
    assert final_manifest["repair_history"][-1]["source_hashes"][
        "manifest.json"
    ] == repair._file_sha256(archive / source.name / "manifest.json")


def test_repair_restores_vertex_gemma_settings(monkeypatch):
    monkeypatch.setattr(repair.os, "environ", {})
    restored = repair._restored_agent_environment({
        "adapter": "codex_vertex_gemma4_26b",
        "provider": "google_vertex_agent_platform",
        "model": "google/gemma-4-26b-a4b-it-maas",
        "project_id": "benchmark-project",
        "region": "global",
        "effort_level": None,
    })

    assert restored["VERTEX_GEMMA_MODEL"] == "google/gemma-4-26b-a4b-it-maas"
    assert restored["VERTEX_GEMMA_PROJECT_ID"] == "benchmark-project"
    assert restored["VERTEX_GEMMA_LOCATION"] == "global"
    assert "CODEX_REASONING_EFFORT" not in restored
