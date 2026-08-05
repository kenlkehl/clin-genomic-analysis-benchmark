"""Tests for model-agnostic agent-stage retries."""

from __future__ import annotations

import json
import subprocess

from clin_genomic_analysis_benchmark.agent import runner
from clin_genomic_analysis_benchmark.concepts import concept_menu_payload


def _question() -> dict:
    return {
        "contract_version": "2",
        "question_id": "bladder_1.2-Qabc12345",
        "question_text": "What is the most common race?",
        "cohort": "bladder_1.2",
        "category": 1,
        "stage": "classify",
        "cohort_dir": "/abs/cohort",
        "data_dictionary_path": "/abs/dictionary.xlsx",
        "scratch_dir": "/abs/scratch",
        "instructions": "Classify the question.",
        "disambiguation_concept_menu": concept_menu_payload(),
    }


def test_invoke_retries_invalid_response_then_succeeds(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            result_path.write_text(json.dumps({"classification": "maybe"}))
        else:
            result_path.write_text(json.dumps({
                "classification": "ambiguous",
                "rationale": "Material choices remain.",
            }))
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=f"attempt {calls}", stderr=""
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    invocation = runner.invoke(
        agent_cmd="fake-agent",
        question_payload=_question(),
        question_path=tmp_path / "question.json",
        result_path=result_path,
        stderr_log_path=tmp_path / "agent.log",
        timeout_s=10,
        max_attempts=3,
        retry_base_seconds=0,
    )

    assert invocation.success is True
    assert invocation.attempt_count == 2
    assert [attempt.success for attempt in invocation.attempts] == [False, True]
    assert invocation.attempts[0].failure_reason == "schema_violation"
    assert invocation.result == {
        "classification": "ambiguous",
        "rationale": "Material choices remain.",
    }
    log = (tmp_path / "agent.log").read_text()
    assert "=== ATTEMPT 1/3 ===" in log
    assert "=== ATTEMPT 2/3 ===" in log


def test_invoke_exhausts_attempts_and_records_each_failure(tmp_path, monkeypatch):
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="", stderr="provider unavailable"
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    invocation = runner.invoke(
        agent_cmd="fake-agent",
        question_payload=_question(),
        question_path=tmp_path / "question.json",
        result_path=tmp_path / "result.json",
        stderr_log_path=tmp_path / "agent.log",
        timeout_s=10,
        max_attempts=3,
        retry_base_seconds=0,
    )

    assert invocation.success is False
    assert invocation.failure_reason == "exit_code=1"
    assert invocation.attempt_count == 3
    assert len(invocation.attempts) == 3
    assert all(attempt.failure_reason == "exit_code=1"
               for attempt in invocation.attempts)
    assert (tmp_path / "agent.log").read_text().count("provider unavailable") == 3
