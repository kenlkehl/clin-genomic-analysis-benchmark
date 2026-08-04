"""Tests for safe, reproducible agent metadata in run manifests."""

from __future__ import annotations

import json

from clin_genomic_analysis_benchmark.agent.orchestrator import _agent_provenance


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


def test_non_claude_adapter_only_gets_allowlisted_environment():
    provenance = _agent_provenance(
        "bash adapters/custom/run.sh",
        environ={
            "CODEX_MODEL": "example-model",
            "AZURE_OPENAI_API_KEY": "must-not-appear",
        },
    )

    assert provenance == {"environment": {"CODEX_MODEL": "example-model"}}
