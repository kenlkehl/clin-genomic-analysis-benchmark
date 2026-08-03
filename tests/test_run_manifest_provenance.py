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
    assert provenance["project_id"] == "benchmark-project"
    assert provenance["project_id_source"] == "ANTHROPIC_VERTEX_PROJECT_ID"
    assert provenance["region"] == "global"
    assert provenance["region_source"] == "CLOUD_ML_REGION"
    assert provenance["environment"] == {
        "CLINGEN_CLAUDE_MODEL": "claude-haiku-4-5@20251001",
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


def test_non_claude_adapter_only_gets_allowlisted_environment():
    provenance = _agent_provenance(
        "bash adapters/custom/run.sh",
        environ={
            "CODEX_MODEL": "example-model",
            "AZURE_OPENAI_API_KEY": "must-not-appear",
        },
    )

    assert provenance == {"environment": {"CODEX_MODEL": "example-model"}}
