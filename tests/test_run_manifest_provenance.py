"""Tests for safe, reproducible agent metadata in run manifests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from clin_genomic_analysis_benchmark.agent import orchestrator
from clin_genomic_analysis_benchmark.agent.orchestrator import (
    QuestionRun,
    _agent_provenance,
)
from clin_genomic_analysis_benchmark.questions.schema import PublicQuestion


def test_claude_vertex_provenance_records_effective_runtime():
    env = {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "benchmark-project",
        "CLOUD_ML_REGION": "global",
        "CLINGEN_CLAUDE_MODEL": "claude-haiku-4-5@20251001",
        "CLINGEN_CLAUDE_EFFORT": "xhigh",
        "ANTHROPIC_API_KEY": "must-not-appear",
        "GOOGLE_APPLICATION_CREDENTIALS": "/secret/credential.json",
    }

    provenance = _agent_provenance(
        "bash adapters/claude_code/run.sh",
        environ=env,
    )

    assert provenance["adapter"] == "claude_code"
    assert provenance["provider"] == "google_vertex_ai"
    assert provenance["model"] == "claude-haiku-4-5@20251001"
    assert provenance["model_source"] == "CLINGEN_CLAUDE_MODEL"
    assert provenance["effort_level"] is None
    assert provenance["effort_supported"] is False
    assert provenance["effort_source"] == "unsupported_by_model"
    assert provenance["project_id"] == "benchmark-project"
    assert provenance["project_id_source"] == "ANTHROPIC_VERTEX_PROJECT_ID"
    assert provenance["region"] == "global"
    assert provenance["region_source"] == "CLOUD_ML_REGION"
    assert provenance["environment"] == {
        "CLINGEN_CLAUDE_MODEL": "claude-haiku-4-5@20251001",
        "CLINGEN_CLAUDE_EFFORT": "xhigh",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "benchmark-project",
        "CLOUD_ML_REGION": "global",
    }
    serialized = json.dumps(provenance)
    assert "must-not-appear" not in serialized
    assert "credential.json" not in serialized


def test_claude_provenance_records_adapter_defaults():
    provenance = _agent_provenance(
        "bash adapters/claude_code/run.sh",
        environ={},
    )

    assert provenance["provider"] == "google_vertex_ai"
    assert provenance["model"] == "claude-opus-4-8"
    assert provenance["model_source"] == "adapter_default"
    assert provenance["project_id"] == "kehllab-caia-v2"
    assert provenance["project_id_source"] == "adapter_default"
    assert provenance["region"] is None
    assert provenance["region_source"] == "unknown"
    assert provenance["effort_level"] is None
    assert provenance["effort_supported"] is True
    assert provenance["effort_source"] == "model_default_unpinned"


def test_claude_provenance_records_explicit_effort():
    provenance = _agent_provenance(
        "bash adapters/claude_code/run.sh",
        environ={
            "CLINGEN_CLAUDE_MODEL": "claude-sonnet-5@20260203",
            "CLINGEN_CLAUDE_EFFORT": "xhigh",
        },
    )

    assert provenance["effort_level"] == "xhigh"
    assert provenance["effort_supported"] is True
    assert provenance["effort_source"] == "CLINGEN_CLAUDE_EFFORT"


def test_codex_provenance_records_explicit_runtime_without_secrets():
    provenance = _agent_provenance(
        "bash adapters/codex_gpt/run.sh",
        environ={
            "CODEX_MODEL": "gpt-5.6-sol",
            "CODEX_MODEL_PROVIDER": "azure",
            "CODEX_REASONING_EFFORT": "xhigh",
            "AZURE_OPENAI_API_KEY": "must-not-appear",
        },
    )

    assert provenance["adapter"] == "codex_gpt"
    assert provenance["model"] == "gpt-5.6-sol"
    assert provenance["model_source"] == "CODEX_MODEL"
    assert provenance["provider"] == "azure"
    assert provenance["provider_source"] == "CODEX_MODEL_PROVIDER"
    assert provenance["effort_level"] == "xhigh"
    assert provenance["effort_supported"] is True
    assert provenance["effort_source"] == "CODEX_REASONING_EFFORT"
    assert provenance["environment"] == {
        "CODEX_MODEL": "gpt-5.6-sol",
        "CODEX_MODEL_PROVIDER": "azure",
        "CODEX_REASONING_EFFORT": "xhigh",
    }
    assert "must-not-appear" not in json.dumps(provenance)


def test_codex_provenance_resolves_profile_over_base_config(tmp_path):
    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.6-terra"\n'
        'model_provider = "openai"\n'
        'model_reasoning_effort = "medium"\n'
        'profile = "azure"\n'
    )
    (tmp_path / "azure.config.toml").write_text(
        'model_provider = "azure"\n'
        'model_reasoning_effort = "xhigh"\n'
    )

    provenance = _agent_provenance(
        "bash adapters/codex_gpt/run.sh",
        environ={
            "CODEX_HOME": str(tmp_path),
            "CODEX_MODEL": "gpt-5.6-luna",
        },
    )

    assert provenance["model"] == "gpt-5.6-luna"
    assert provenance["model_source"] == "CODEX_MODEL"
    assert provenance["provider"] == "azure"
    assert provenance["provider_source"] == "codex_profile:azure"
    assert provenance["effort_level"] == "xhigh"
    assert provenance["effort_source"] == "codex_profile:azure"
    assert provenance["profile"] == "azure"
    assert provenance["profile_source"] == "codex_user_config"
    assert "CODEX_HOME" not in provenance["environment"]


def test_antigravity_provenance_records_pinned_variant_without_secrets(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "gcp": {"project": "settings-project", "location": "us"},
        "trustedWorkspaces": ["/secret/gold"],
    }))
    provenance = _agent_provenance(
        "bash adapters/antigravity_gemini/run.sh",
        environ={
            "CLINGEN_AGY_CONFIG_DIR": str(tmp_path),
            "AGY_MODEL": "gemini-3.6-flash-high",
            "AGY_EFFORT": "medium",
            "AGY_MODE": "accept-edits",
            "GOOGLE_APPLICATION_CREDENTIALS": "/secret/credential.json",
        },
    )

    assert provenance["adapter"] == "antigravity_gemini"
    assert provenance["provider"] == "google_antigravity"
    assert provenance["model"] == "gemini-3.6-flash-high"
    assert provenance["model_source"] == "AGY_MODEL"
    assert provenance["effort_level"] == "medium"
    assert provenance["effort_supported"] is True
    assert provenance["effort_source"] == "AGY_EFFORT"
    assert provenance["project_id"] == "settings-project"
    assert provenance["region"] == "us"
    assert provenance["mode"] == "accept-edits"
    assert provenance["cli_auto_update"] is False
    assert provenance["inner_sandbox"] is False
    assert provenance["inner_sandbox_source"] == "adapter_default"
    assert provenance["environment"] == {
        "AGY_MODEL": "gemini-3.6-flash-high",
        "AGY_EFFORT": "medium",
        "AGY_MODE": "accept-edits",
    }
    serialized = json.dumps(provenance)
    assert "credential.json" not in serialized
    assert "/secret/gold" not in serialized


def test_non_claude_adapter_only_gets_allowlisted_environment():
    provenance = _agent_provenance(
        "bash adapters/custom/run.sh",
        environ={
            "CODEX_MODEL": "example-model",
            "AZURE_OPENAI_API_KEY": "must-not-appear",
        },
    )

    assert provenance == {"environment": {"CODEX_MODEL": "example-model"}}


def test_run_manifest_records_passed_isolation_checks(tmp_path, monkeypatch):
    cohort = SimpleNamespace(name="c", path=tmp_path / "cohort")
    cohort.path.mkdir()
    question = PublicQuestion(id="c-Q1", category=1, text="Question?")
    bank = SimpleNamespace(questions=[question])
    monkeypatch.setattr(orchestrator, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(orchestrator, "resolve_cohorts", lambda spec: [cohort])
    monkeypatch.setattr(orchestrator.q_io, "load_public", lambda name: bank)
    monkeypatch.setattr(
        orchestrator,
        "require_supported_adapter",
        lambda command: "claude_code",
    )
    monkeypatch.setattr(
        orchestrator,
        "sandbox_backend_provenance",
        lambda: {"backend": "bubblewrap", "mode": "required_fail_closed"},
    )
    monkeypatch.setattr(
        orchestrator,
        "run_isolation_preflight",
        lambda: {"passed": True},
    )
    monkeypatch.setattr(orchestrator, "audit_agent_artifacts", lambda root: [])
    monkeypatch.setattr(
        orchestrator,
        "_run_one_question",
        lambda **kwargs: QuestionRun(
            question_id="c-Q1",
            cohort="c",
            category=1,
            classification_gold="",
        ),
    )

    run_dir = orchestrator.run_eval(
        agent_cmd="bash adapters/claude_code/run.sh",
        agent_name="claude",
        max_parallel=1,
        run_id="isolated-run",
    )

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["integrity"]["status"] == "valid"
    assert manifest["integrity"]["adapter"] == "claude_code"
    assert manifest["integrity"]["sandbox"]["backend"] == "bubblewrap"
    assert manifest["integrity"]["preflight"]["passed"] is True
    assert manifest["integrity"]["postflight"]["passed"] is True
