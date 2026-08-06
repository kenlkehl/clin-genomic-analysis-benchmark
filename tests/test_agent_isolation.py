"""Regression tests for the model-CLI confidentiality boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clin_genomic_analysis_benchmark.agent import isolation


pytestmark = pytest.mark.skipif(
    not isolation.BWRAP.is_file(), reason="mandatory bubblewrap is not installed"
)


def _mounts(tmp_path: Path) -> tuple[Path, Path, Path]:
    cohort = tmp_path / "cohort"
    scratch = tmp_path / "scratch"
    dictionary = tmp_path / "variables.xlsx"
    cohort.mkdir()
    scratch.mkdir()
    dictionary.write_text("dictionary")
    (cohort / "patients.csv").write_text("id\n1\n")
    return cohort, dictionary, scratch


def test_bubblewrap_exposes_only_current_inputs_and_scratch(tmp_path):
    cohort, dictionary, scratch = _mounts(tmp_path)
    forbidden = tmp_path / "gold-answer.txt"
    forbidden.write_text("the answer is 42")
    script = """
import json, os, sys
payload = {
    "cohort": open("/data/cohort/patients.csv").read().strip(),
    "dictionary_visible": os.path.isfile("/data/dictionary/variables.xlsx"),
    "scratch_writable": os.access("/work", os.W_OK),
    "forbidden_visible": os.path.exists(sys.argv[1]),
    "host_home_visible": os.path.exists("/home/klkehl"),
    "scratch_host_path_in_mountinfo": sys.argv[2].encode() in open(
        "/proc/self/mountinfo", "rb"
    ).read(),
}
open("/work/proof.json", "w").write(json.dumps(payload))
"""
    with isolation.sandboxed_agent_command(
        [
            "/usr/bin/python3", "-c", script,
            str(forbidden.resolve()), str(scratch.resolve()),
        ],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={},
        home_kind="none",
    ) as launch:
        proc = subprocess.run(
            launch.command,
            env=launch.environment,
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert proc.returncode == 0, proc.stderr
    proof = json.loads((scratch / "proof.json").read_text())
    assert proof == {
        "cohort": "id\n1",
        "dictionary_visible": True,
        "scratch_writable": True,
        "forbidden_visible": False,
        "host_home_visible": False,
        "scratch_host_path_in_mountinfo": False,
    }


def test_environment_and_prompt_paths_are_scrubbed(tmp_path):
    cohort, dictionary, scratch = _mounts(tmp_path)
    question = {
        "cohort_dir": str(cohort),
        "data_dictionary_path": str(dictionary),
        "scratch_dir": str(scratch),
        "stage": "classify",
    }
    view = isolation.sandbox_question_view(question)
    assert view["cohort_dir"] == "/data/cohort"
    assert view["data_dictionary_path"] == "/data/dictionary/variables.xlsx"
    assert view["scratch_dir"] == "/work"
    assert str(tmp_path) not in json.dumps(view)

    with isolation.sandboxed_agent_command(
        ["/usr/bin/true"],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={
            "CLINGEN_GOLD_ROOT": "/secret/gold",
            "UNRELATED_SECRET": "hidden",
            "AZURE_OPENAI_API_KEY": "needed-by-provider",
        },
        home_kind="none",
    ) as launch:
        assert "CLINGEN_GOLD_ROOT" not in launch.environment
        assert "UNRELATED_SECRET" not in launch.environment
        assert launch.environment["AZURE_OPENAI_API_KEY"] == "needed-by-provider"
        assert launch.environment["HOME"] == "/home/agent"


def test_codex_home_excludes_projects_and_session_history(tmp_path):
    cohort, dictionary, scratch = _mounts(tmp_path)
    source_home = tmp_path / "host-codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'model = "gpt-test"\n'
        'model_provider = "azure"\n'
        '[model_providers.azure]\n'
        'name = "Azure"\n'
        'base_url = "https://example.invalid/openai/v1"\n'
        'env_key = "AZURE_OPENAI_API_KEY"\n'
        '[projects."/host/path/with/answers"]\n'
        'trust_level = "trusted"\n'
    )
    (source_home / "history.jsonl").write_text("prior session")

    with isolation.sandboxed_agent_command(
        ["/usr/bin/true"],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={"CODEX_HOME": str(source_home)},
        home_kind="codex",
    ) as launch:
        home_destination_index = launch.command.index("/home/agent")
        sandbox_home = Path(launch.command[home_destination_index - 1])
        config = (sandbox_home / ".codex/config.toml").read_text()
        assert "gpt-test" in config
        assert "https://example.invalid/openai/v1" in config
        assert "projects" not in config
        assert "/host/path/with/answers" not in config
        assert not (sandbox_home / ".codex/history.jsonl").exists()


def test_codex_vertex_home_excludes_user_config_and_auth(tmp_path):
    cohort, dictionary, scratch = _mounts(tmp_path)
    source_home = tmp_path / "host-codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'model = "private-model"\n'
        '[projects."/host/path/with/answers"]\n'
        'trust_level = "trusted"\n'
    )
    (source_home / "auth.json").write_text('{"tokens":"must-not-copy"}')

    with isolation.sandboxed_agent_command(
        ["/usr/bin/true"],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={
            "CODEX_HOME": str(source_home),
            "CODEX_MODEL": "google/gemma-4-26b-a4b-it-maas",
            "CODEX_MODEL_PROVIDER": "google_vertex_agent_platform",
        },
        home_kind="codex_vertex",
    ) as launch:
        config = (launch.host_ephemeral_home / ".codex/config.toml").read_text()
        assert "google/gemma-4-26b-a4b-it-maas" in config
        assert "google_vertex_agent_platform" in config
        assert "private-model" not in config
        assert "/host/path/with/answers" not in config
        assert not (launch.host_ephemeral_home / ".codex/auth.json").exists()


def test_antigravity_home_excludes_persistent_agent_state(tmp_path):
    cohort, dictionary, scratch = _mounts(tmp_path)
    source = tmp_path / "host-antigravity"
    (source / "cache").mkdir(parents=True)
    (source / "conversations").mkdir()
    (source / "brain").mkdir()
    (source / "settings.json").write_text(json.dumps({
        "model": "old model",
        "gcp": {"project": "settings-project", "location": "us"},
        "trustedWorkspaces": ["/host/path/with/answers"],
        "permissions": {"allow": ["Shell(*)"]},
        "allowNonWorkspaceAccess": True,
    }))
    (source / "cache/onboarding.json").write_text(json.dumps({
        "enterpriseOnboardingComplete": True,
        "unrelated": "do not copy",
    }))
    (source / "history.jsonl").write_text("prior session")
    (source / "conversations/prior.db").write_text("prior conversation")
    (source / "brain/gold.txt").write_text("prior memory")

    with isolation.sandboxed_agent_command(
        ["/usr/bin/true"],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={
            "CLINGEN_AGY_CONFIG_DIR": str(source),
            "AGY_MODEL": "gemini-3.6-flash-high",
            "AGY_GCP_PROJECT": "explicit-project",
            "AGY_GCP_LOCATION": "global",
        },
        home_kind="antigravity",
    ) as launch:
        config_root = launch.host_ephemeral_home / ".gemini/antigravity-cli"
        settings = json.loads((config_root / "settings.json").read_text())
        assert settings == {
            "allowNonWorkspaceAccess": False,
            "artifactReviewPolicy": "always-proceed",
            "enableTerminalSandbox": False,
            "enableTelemetry": False,
            "toolPermission": "always-proceed",
            "permissions": {
                "allow": ["command(*)"],
                "ask": [],
                "deny": [
                    "read_file(/home/agent/.gemini)",
                    "read_file(/home/agent/.config/gcloud)",
                    "read_url(*)",
                    "execute_url(*)",
                    "mcp(*)",
                ],
            },
            "trustedWorkspaces": ["/work"],
            "model": "gemini-3.6-flash-high",
            "gcp": {"project": "explicit-project", "location": "global"},
        }
        onboarding = json.loads((config_root / "cache/onboarding.json").read_text())
        assert onboarding == {"enterpriseOnboardingComplete": True}
        assert not (config_root / "history.jsonl").exists()
        assert not (config_root / "conversations").exists()
        assert not (config_root / "brain").exists()
        assert "/host/path/with/answers" not in json.dumps(settings)


def test_antigravity_gets_only_its_oauth_file_fallback(tmp_path, monkeypatch):
    cohort, dictionary, scratch = _mounts(tmp_path)
    profile = json.dumps({
        "auth_method": "oauth",
        "token": {
            "access_token": "test-access",
            "refresh_token": "test-refresh",
        },
    }).encode()
    monkeypatch.setattr(
        isolation,
        "_lookup_antigravity_keyring_secret",
        lambda source_env: profile,
    )

    with isolation.sandboxed_agent_command(
        ["/usr/bin/true"],
        cohort_dir=cohort,
        data_dictionary_path=dictionary,
        scratch_dir=scratch,
        environment={
            "AGY_MODEL": "gemini-3.6-flash-high",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/session/bus",
        },
        home_kind="antigravity",
    ) as launch:
        token_file = (
            launch.host_ephemeral_home
            / ".gemini/antigravity-cli/antigravity-oauth-token"
        )
        assert token_file.read_bytes() == profile
        assert token_file.stat().st_mode & 0o777 == 0o600
        assert "DBUS_SESSION_BUS_ADDRESS" not in launch.environment


def test_preflight_and_forbidden_marker_audit(tmp_path):
    assert isolation.run_isolation_preflight()["passed"] is True
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "response.json").write_text('{"classification":"ambiguous"}')
    assert isolation.audit_agent_artifacts(clean) == []

    (clean / "agent.log").write_text("I opened scorecard.json")
    findings = isolation.audit_agent_artifacts(clean)
    assert findings == [{"path": "agent.log", "marker": "scorecard.json"}]

    (clean / "agent.log").unlink()
    (clean / "model-link").symlink_to(tmp_path / "outside")
    assert isolation.audit_agent_artifacts(clean) == [{
        "path": "model-link",
        "marker": "<symlink-artifact>",
    }]


def test_unregistered_adapter_fails_closed():
    with pytest.raises(isolation.AgentIsolationError, match="not registered"):
        isolation.require_supported_adapter("bash adapters/custom/run.sh")
    with pytest.raises(isolation.AgentIsolationError, match="not registered"):
        isolation.require_supported_adapter(
            "evil-agent --label adapters/claude_code/run.sh"
        )


def test_antigravity_adapter_is_registered():
    command = "bash adapters/antigravity_gemini/run.sh"
    assert isolation.require_supported_adapter(command) == "antigravity_gemini"
