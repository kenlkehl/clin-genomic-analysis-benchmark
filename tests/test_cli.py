"""CLI coverage: assess, analyze with automatic repair, retries, and combine."""

from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from clin_genomic_analysis_benchmark.agent.repair import RepairTarget
from clin_genomic_analysis_benchmark.cli import cli


def _loop(final_run_dir, target=None):
    passes = () if target is None else (SimpleNamespace(
        repaired_run_dir=final_run_dir, successful_merges=1, failed_retries=0,
        targets=(target,)),)
    return SimpleNamespace(final_run_dir=final_run_dir, stop_reason="complete",
                           remaining_targets=(), passes=passes)


def _run_with_task(tmp_path, task: str):
    run_dir = tmp_path / "runs" / "name" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"task": task}))
    (run_dir / "runs.json").write_text("[]")
    return run_dir


def test_analyze_automatically_repairs_score_relevant_failures(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import orchestrator, repair

    run_dir = tmp_path / "runs" / "agent" / "raw-run"
    repaired_dir = run_dir.parent / "repaired-run"
    target = RepairTarget("q", "c", "timeout")
    monkeypatch.setattr(orchestrator, "run_analyze", lambda **kwargs: run_dir)
    monkeypatch.setattr(repair, "plan_failed_run", lambda path: (run_dir, [target]))
    calls = []
    monkeypatch.setattr(repair, "retry_failed_run_until_stable",
                        lambda **kwargs: calls.append(kwargs) or _loop(repaired_dir, target))

    result = CliRunner().invoke(cli, [
        "analyze", "--agent", "fake-agent", "--agent-name", "agent", "--max-parallel", "2",
        "--repair-analyze-timeout", "240",
    ])

    assert result.exit_code == 0, result.output
    assert "Post-run repair: retrying 1 failed question(s)." in result.output
    assert "Post-run repair pass 1 merged 1/1 successful retries." in result.output
    assert f"Final repaired run: {repaired_dir}" in result.output
    assert calls[0]["run_path"] == run_dir
    assert calls[0]["agent_cmd"] == "fake-agent"
    assert calls[0]["max_parallel"] == 2
    assert calls[0]["agent_max_attempts"] == 10
    assert calls[0]["max_repair_passes"] == 10
    assert calls[0]["timeout_overrides"] == {"analyze": 240}


def test_analyze_automatically_scores_clean_run(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import orchestrator, repair
    from clin_genomic_analysis_benchmark.scoring import driver

    run_dir = tmp_path / "runs" / "agent" / "clean-run"
    monkeypatch.setattr(orchestrator, "run_analyze", lambda **kwargs: run_dir)
    monkeypatch.setattr(repair, "plan_failed_run", lambda path: (run_dir, []))
    calls = []
    monkeypatch.setattr(driver, "score_run", lambda **kwargs: calls.append(kwargs) or run_dir)

    result = CliRunner().invoke(cli, ["analyze", "--agent", "fake-agent",
                                      "--agent-name", "agent"])

    assert result.exit_code == 0, result.output
    assert "Post-run repair: no score-relevant technical failures." in result.output
    assert f"Scorecard: {run_dir / 'scorecard.md'}" in result.output
    assert calls[0]["run_path"] == str(run_dir)
    assert calls[0]["scoring_config_path"].name == "default.yaml"


def test_analyze_can_explicitly_disable_post_run_repair(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import orchestrator, repair
    from clin_genomic_analysis_benchmark.scoring import driver

    run_dir = tmp_path / "raw-run"
    monkeypatch.setattr(orchestrator, "run_analyze", lambda **kwargs: run_dir)
    score_calls = []
    monkeypatch.setattr(driver, "score_run",
                        lambda **kwargs: score_calls.append(kwargs) or run_dir)

    def unexpected_plan(path):
        raise AssertionError("repair planning should be disabled")

    monkeypatch.setattr(repair, "plan_failed_run", unexpected_plan)

    result = CliRunner().invoke(cli, ["analyze", "--agent", "fake-agent",
                                      "--agent-name", "agent", "--no-retry-failures"])

    assert result.exit_code == 0, result.output
    assert f"Analyze run written to {run_dir}" in result.output
    assert "Post-run repair" not in result.output
    assert score_calls[0]["run_path"] == str(run_dir)


def test_retired_commands_and_flags_are_rejected():
    runner = CliRunner()
    assert runner.invoke(cli, ["eval", "--agent", "a", "--agent-name", "a"]).exit_code != 0
    for flag in ("--stages", "--repair-assess-timeout", "--repair-classify-timeout"):
        retired = runner.invoke(cli, ["analyze", "--agent", "a", "--agent-name", "a",
                                      flag, "5"])
        assert retired.exit_code != 0
        assert "No such option" in retired.output


def test_retry_failures_repairs_an_analyze_run(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import repair

    run_dir = _run_with_task(tmp_path, "analyze")
    target = RepairTarget("q", "c", "timeout")
    monkeypatch.setattr(repair, "plan_failed_run", lambda path: (run_dir, [target]))
    calls = []
    monkeypatch.setattr(repair, "retry_failed_run_until_stable",
                        lambda **kwargs: calls.append(kwargs) or _loop(run_dir))

    result = CliRunner().invoke(cli, ["retry-failures", "--run", str(run_dir),
                                      "--analyze-timeout", "300"])

    assert result.exit_code == 0, result.output
    assert "c/q (timeout)" in result.output
    assert calls[0]["timeout_overrides"] == {"analyze": 300}
    assert calls[0]["agent_max_attempts"] == 10
    assert CliRunner().invoke(cli, ["retry-failures", "--run", str(run_dir),
                                    "--assess-timeout", "5"]).exit_code != 0


def test_retry_failures_retries_an_assess_run(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.assess import runner as assess_runner
    from clin_genomic_analysis_benchmark.scoring import driver

    run_dir = _run_with_task(tmp_path, "assess")
    (run_dir / "runs.json").write_text(json.dumps([
        {"cohort": "c", "question_id": "q", "assess": {"success": False,
                                                        "failure_reason": "empty reply"}},
        {"cohort": "c", "question_id": "ok", "assess": {"success": True}},
    ]))
    retried = run_dir.parent / "run-retry"
    calls = []
    monkeypatch.setattr(assess_runner, "retry_assess_failures", lambda **kwargs: calls.append(
        kwargs) or SimpleNamespace(retried_run_dir=retried, n_targets=1, n_recovered=1))
    scored = []
    monkeypatch.setattr(driver, "score_run", lambda **kwargs: scored.append(kwargs) or retried)

    result = CliRunner().invoke(cli, ["retry-failures", "--run", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "Failed assess calls: 1" in result.output
    assert "c/q (empty reply)" in result.output
    assert "Recovered 1/1 question(s)." in result.output
    assert calls[0]["max_attempts"] == 5
    assert scored[0]["run_path"] == str(retried)

    dry = CliRunner().invoke(cli, ["retry-failures", "--run", str(run_dir), "--dry-run"])
    assert dry.exit_code == 0 and len(calls) == 1
    rejected = CliRunner().invoke(cli, ["retry-failures", "--run", str(run_dir),
                                        "--agent", "x"])
    assert rejected.exit_code != 0
    assert "apply only to analyze runs" in rejected.output


def test_assess_builds_the_backend_from_flags_and_scores(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.assess import runner as assess_runner
    from clin_genomic_analysis_benchmark.scoring import driver

    run_dir = tmp_path / "runs" / "assess_x" / "run"
    calls = []
    monkeypatch.setattr(assess_runner, "run_assess",
                        lambda **kwargs: calls.append(kwargs) or run_dir)
    scored = []
    monkeypatch.setattr(driver, "score_run", lambda **kwargs: scored.append(kwargs) or run_dir)

    result = CliRunner().invoke(cli, [
        "assess", "--model", "openai-compatible:google/gemma-4", "--cohort", "bladder_1.2",
        "--base-url", "http://127.0.0.1:8000/v1", "--max-tokens", "4096",
        "--params", '{"reasoning_effort": "high"}', "--max-parallel", "8",
    ])

    assert result.exit_code == 0, result.output
    config = calls[0]["backend_config"]
    assert (config.provider, config.model) == ("openai-compatible", "google/gemma-4")
    assert config.base_url == "http://127.0.0.1:8000/v1"
    assert config.max_tokens == 4096
    assert config.params == {"reasoning_effort": "high"}
    assert calls[0]["run_name"] == "assess_openai-compatible-google-gemma-4"
    assert calls[0]["cohort_spec"] == "bladder_1.2"
    assert calls[0]["max_parallel"] == 8
    assert scored[0]["run_path"] == str(run_dir)


def test_assess_rejects_a_bad_model_spec_or_params():
    for args in (["--model", "claude-opus-5-5"],
                 ["--model", "made-up:model"],
                 ["--model", "azure-openai:gpt-5", "--params", "[1, 2]"]):
        result = CliRunner().invoke(cli, ["assess", *args])
        assert result.exit_code != 0, args


def test_combine_calls_combine_runs(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.scoring import combine

    out = tmp_path / "runs" / "combined" / "c1"
    calls = []
    monkeypatch.setattr(combine, "combine_runs", lambda **kwargs: calls.append(kwargs) or out)

    result = CliRunner().invoke(cli, ["combine", "--assess-run", "a/1", "--analyze-run",
                                      "b/2", "--allow-mismatch"])

    assert result.exit_code == 0, result.output
    assert calls[0]["assess_run"] == "a/1"
    assert calls[0]["analyze_run"] == "b/2"
    assert calls[0]["allow_mismatch"] is True
    assert f"Combined scorecard written to {out / 'scorecard.md'}" in result.output
