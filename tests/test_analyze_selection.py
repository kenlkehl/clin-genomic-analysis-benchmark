"""The analyze task runs an agent on gold-unambiguous questions only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clin_genomic_analysis_benchmark import cohorts
from clin_genomic_analysis_benchmark.agent import contract, orchestrator
from clin_genomic_analysis_benchmark.agent.runner import AgentInvocation
from clin_genomic_analysis_benchmark.config import TimeoutConfig
from clin_genomic_analysis_benchmark.questions.schema import (
    PublicCohortQuestionFile,
    PublicQuestion,
)

_COHORT = cohorts.Cohort(name="cohort_1", label="Cohort 1", cancer_type="test",
                         release="v0", disease="test cancer")


def _fake_invoke(payloads: list[dict]):
    def fake(*, question_payload: dict, **_kwargs) -> AgentInvocation:
        assert contract.validate_question(question_payload) == []
        payloads.append(question_payload)
        return AgentInvocation(
            success=True, exit_code=0, timed_out=False, duration_seconds=0.0,
            result={"answer_type": "count", "answer": {"value": 1, "n": 1}},
            schema_errors=[], failure_reason=None, stdout="", stderr="",
            attempt_count=1, attempts=[])
    return fake


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Two public questions; gold says only Q1 is unambiguous."""
    payloads: list[dict] = []
    monkeypatch.setattr(orchestrator, "invoke", _fake_invoke(payloads))
    monkeypatch.setattr(orchestrator, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(orchestrator, "resolve_cohorts", lambda spec: [_COHORT])
    monkeypatch.setattr(orchestrator, "require_supported_adapter", lambda cmd: "adapter")
    monkeypatch.setattr(orchestrator, "sandbox_backend_provenance", lambda: {})
    monkeypatch.setattr(orchestrator, "run_isolation_preflight", lambda: {"passed": True})
    monkeypatch.setattr(orchestrator, "audit_agent_artifacts", lambda path: [])
    monkeypatch.setattr(orchestrator, "_agent_provenance", lambda cmd: {"adapter": "adapter"})
    monkeypatch.setattr(cohorts, "find_data_dictionary", lambda c: tmp_path / "dict.xlsx")
    public = PublicCohortQuestionFile(
        cohort="cohort_1", generated_at="2026-01-01T00:00:00Z", model="m",
        questions=[PublicQuestion(id="cohort_1-Q1", category=1, text="How many?"),
                   PublicQuestion(id="cohort_1-Q2", category=2, text="How did they do?")])
    gold = SimpleNamespace(questions=[
        SimpleNamespace(id="cohort_1-Q1", classification="unambiguous"),
        SimpleNamespace(id="cohort_1-Q2", classification="ambiguous"),
    ])
    monkeypatch.setattr(orchestrator.q_io, "load_public", lambda name: public)
    monkeypatch.setattr(orchestrator.q_io, "load_gold", lambda name: gold)
    return payloads


def test_payload_is_analyze_only(tmp_path, monkeypatch):
    payloads: list[dict] = []
    monkeypatch.setattr(orchestrator, "invoke", _fake_invoke(payloads))
    monkeypatch.setattr(cohorts, "find_data_dictionary", lambda c: tmp_path / "dict.xlsx")
    q = PublicQuestion(id="cohort_1-Q1", category=1, text="How many patients?")
    qrun = orchestrator._run_one_question(
        agent_cmd="adapter", q=q, cohort=_COHORT, run_dir=tmp_path,
        agent_max_attempts=1, agent_retry_base_seconds=0.0,
        timeout_config=TimeoutConfig(analyze=10))
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["contract_version"] == contract.CONTRACT_VERSION
    for retired in ("stage", "assessment_components", "prior_classification"):
        assert retired not in payload
    assert payload["max_runtime_seconds"] == 10
    assert qrun.analyze["success"] is True
    assert not hasattr(qrun, "assess")


def test_run_analyze_selects_gold_unambiguous_questions(harness):
    run_dir = orchestrator.run_analyze(agent_cmd="adapter", agent_name="a", max_parallel=1)
    assert [p["question_id"] for p in harness] == ["cohort_1-Q1"]
    runs = json.loads((run_dir / "runs.json").read_text())
    assert [r["question_id"] for r in runs] == ["cohort_1-Q1"]
    assert set(runs[0]) == {"question_id", "cohort", "category", "analyze", "error"}
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["task"] == "analyze"
    assert manifest["settings"]["selection"] == "gold_unambiguous"
    assert set(manifest["rulebook"]) == {"ANALYZE_INSTRUCTIONS.md", "CONVENTIONS.md"}
    assert "stages" not in manifest["settings"]


def test_run_analyze_refuses_an_ambiguous_question(harness):
    with pytest.raises(RuntimeError, match="not gold-unambiguous"):
        orchestrator.run_analyze(agent_cmd="adapter", agent_name="a",
                                 question_id="cohort_1-Q2")
    assert harness == []


def test_run_analyze_needs_the_gold_bank(harness, monkeypatch):
    monkeypatch.setattr(orchestrator.q_io, "load_gold", lambda name: None)
    with pytest.raises(RuntimeError, match="no gold bank"):
        orchestrator.run_analyze(agent_cmd="adapter", agent_name="a")
