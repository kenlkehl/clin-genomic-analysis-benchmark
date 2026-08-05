"""CLI coverage for automatic post-eval failure repair."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from clin_genomic_analysis_benchmark.agent.repair import RepairTarget
from clin_genomic_analysis_benchmark.cli import cli


def test_eval_automatically_repairs_score_relevant_failures(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import orchestrator, repair

    run_dir = tmp_path / "runs" / "agent" / "raw-run"
    repaired_dir = run_dir.parent / "repaired-run"
    target = RepairTarget("q", "c", "disambiguate", "timeout")
    monkeypatch.setattr(orchestrator, "run_eval", lambda **kwargs: run_dir)
    monkeypatch.setattr(repair, "plan_failed_run", lambda path: (run_dir, [target]))
    calls = []

    def fake_retry(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            repaired_run_dir=repaired_dir,
            successful_merges=1,
            failed_retries=0,
            targets=(target,),
        )

    monkeypatch.setattr(repair, "retry_failed_run", fake_retry)

    result = CliRunner().invoke(cli, [
        "eval",
        "--agent", "fake-agent",
        "--agent-name", "agent",
        "--max-parallel", "2",
    ])

    assert result.exit_code == 0, result.output
    assert "Post-run repair: retrying 1 failed question(s)." in result.output
    assert f"Final repaired run: {repaired_dir}" in result.output
    assert calls[0]["run_path"] == run_dir
    assert calls[0]["agent_cmd"] == "fake-agent"
    assert calls[0]["max_parallel"] == 2


def test_eval_can_explicitly_disable_post_run_repair(tmp_path, monkeypatch):
    from clin_genomic_analysis_benchmark.agent import orchestrator, repair

    run_dir = tmp_path / "raw-run"
    monkeypatch.setattr(orchestrator, "run_eval", lambda **kwargs: run_dir)

    def unexpected_plan(path):
        raise AssertionError("repair planning should be disabled")

    monkeypatch.setattr(repair, "plan_failed_run", unexpected_plan)

    result = CliRunner().invoke(cli, [
        "eval",
        "--agent", "fake-agent",
        "--agent-name", "agent",
        "--no-retry-failures",
    ])

    assert result.exit_code == 0, result.output
    assert f"Eval written to {run_dir}" in result.output
    assert "Post-run repair" not in result.output
