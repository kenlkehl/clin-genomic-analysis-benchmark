"""The orchestrator routes assess → analyze only when every component is clear."""

from __future__ import annotations

import pytest

from clin_genomic_analysis_benchmark import cohorts
from clin_genomic_analysis_benchmark.agent import contract, orchestrator
from clin_genomic_analysis_benchmark.agent.runner import StageInvocation
from clin_genomic_analysis_benchmark.components import COMPONENT_IDS
from clin_genomic_analysis_benchmark.config import TimeoutConfig
from clin_genomic_analysis_benchmark.questions.schema import PublicQuestion


def _assess_result(*unclear: str) -> dict:
    return {cid: {"needs_clarification": cid in unclear, "rationale": "r"}
            for cid in COMPONENT_IDS}


def _run(tmp_path, monkeypatch, assess_result: dict | None, stages=("assess", "analyze"),
         gold_classification: str | None = None) -> tuple[orchestrator.QuestionRun, list[dict]]:
    payloads: list[dict] = []

    def fake_invoke(*, question_payload: dict, **_kwargs) -> StageInvocation:
        assert contract.validate_question(question_payload) == []
        payloads.append(question_payload)
        stage = question_payload["stage"]
        result = assess_result if stage == "assess" else {
            "answer_type": "count", "answer": {"value": 1, "n": 1}}
        return StageInvocation(
            stage=stage, success=result is not None, exit_code=0, timed_out=False,
            duration_seconds=0.0, result=result, schema_errors=[],
            failure_reason=None if result is not None else "boom",
            stdout="", stderr="", attempt_count=1, attempts=[])

    monkeypatch.setattr(orchestrator, "invoke", fake_invoke)
    monkeypatch.setattr(cohorts, "find_data_dictionary",
                        lambda cohort: tmp_path / "dictionary.xlsx")
    cohort = cohorts.Cohort(name="cohort_1", label="Cohort 1", cancer_type="test",
                            release="v0")
    q = PublicQuestion(id="cohort_1-Q1", category=1, text="How many patients?")
    qrun = orchestrator._run_one_question(
        agent_cmd="adapter", q=q, cohort=cohort, run_dir=tmp_path, stages=list(stages),
        agent_max_attempts=1, agent_retry_base_seconds=0.0,
        gold_classification=gold_classification,
        timeout_config=TimeoutConfig(assess=10, analyze=10))
    return qrun, payloads


def test_all_clear_assess_routes_to_analyze(tmp_path, monkeypatch):
    qrun, payloads = _run(tmp_path, monkeypatch, _assess_result())
    assert [p["stage"] for p in payloads] == ["assess", "analyze"]
    assert payloads[0]["contract_version"] == "3"
    assert payloads[1]["prior_classification"] == "unambiguous"
    assert qrun.assess["success"] and qrun.analyze["success"]


@pytest.mark.parametrize("component", COMPONENT_IDS)
def test_any_flagged_component_stops_after_assess(tmp_path, monkeypatch, component):
    qrun, payloads = _run(tmp_path, monkeypatch, _assess_result(component))
    assert [p["stage"] for p in payloads] == ["assess"]
    assert qrun.analyze is None


def test_failed_assess_does_not_analyze(tmp_path, monkeypatch):
    qrun, payloads = _run(tmp_path, monkeypatch, None)
    assert [p["stage"] for p in payloads] == ["assess"]
    assert qrun.assess["success"] is False
    assert qrun.analyze is None


def test_analyze_only_rerun_follows_gold(tmp_path, monkeypatch):
    qrun, payloads = _run(tmp_path, monkeypatch, None, stages=("analyze",),
                          gold_classification="unambiguous")
    assert [p["stage"] for p in payloads] == ["analyze"]
    assert qrun.assess is None


def test_retired_stage_names_are_rejected(monkeypatch):
    monkeypatch.setattr(orchestrator, "require_supported_adapter", lambda cmd: "adapter")
    monkeypatch.setattr(orchestrator, "sandbox_backend_provenance", lambda: {})
    monkeypatch.setattr(orchestrator, "run_isolation_preflight", lambda: {})
    with pytest.raises(RuntimeError, match="unknown stage"):
        orchestrator.run_eval(agent_cmd="adapter", agent_name="a",
                              stages=["classify", "disambiguate"])
