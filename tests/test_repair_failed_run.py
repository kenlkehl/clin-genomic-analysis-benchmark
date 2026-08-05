"""Tests for selective, non-destructive repair of failed benchmark stages."""

from __future__ import annotations

import json
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
    )

    repaired = source.parent / "repaired-run"
    assert summary.repaired_run_dir == repaired
    assert summary.successful_merges == 1
    assert summary.failed_retries == 0
    assert captured_kwargs["stages"] == ["disambiguate"]
    assert captured_kwargs["gold_classification"] == "ambiguous"
    assert captured_kwargs["timeout_config"].disambiguate == 20

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
