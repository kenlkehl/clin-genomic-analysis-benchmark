"""Focused tests for the model-neutral Antigravity adapter."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "adapters"
    / "antigravity_gemini"
    / "adapter.py"
)
_SPEC = importlib.util.spec_from_file_location("antigravity_adapter", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)


def _question(tmp_path: Path) -> dict:
    cohort = tmp_path / "cohort"
    scratch = tmp_path / "question" / "scratch"
    dictionary = tmp_path / "dictionary.xlsx"
    cohort.mkdir(exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    dictionary.write_text("dictionary")
    return {
        "question_id": "cohort-Q1",
        "cohort": "cohort",
        "category": 1,
        "stage": "classify",
        "cohort_dir": str(cohort),
        "data_dictionary_path": str(dictionary),
        "scratch_dir": str(scratch),
        "question_text": "Question?",
        "instructions": "Return the classification.",
    }


@pytest.fixture
def _stub_outer_sandbox(monkeypatch, tmp_path):
    ephemeral_home = tmp_path / "ephemeral-home"
    ephemeral_home.mkdir()

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        yield SimpleNamespace(
            command=cmd,
            environment=kwargs["environment"],
            host_ephemeral_home=ephemeral_home,
        )

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(
        adapter, "export_agent_session_audit", lambda *args, **kwargs: []
    )


def test_antigravity_requires_an_explicit_model(monkeypatch, tmp_path):
    monkeypatch.delenv("AGY_MODEL", raising=False)
    with pytest.raises(adapter.AntigravityInvocationError, match="AGY_MODEL is required"):
        adapter._agy_call(prompt="prompt", question=_question(tmp_path))


def test_antigravity_passes_model_and_canonical_paths(
    monkeypatch, tmp_path, _stub_outer_sandbox
):
    captured = {}
    completed = subprocess.CompletedProcess(
        args=["agy"], returncode=0, stdout='{"classification":"ambiguous"}', stderr=""
    )
    monkeypatch.setenv("AGY_MODEL", "gemini-3.6-flash-high")
    monkeypatch.setenv("AGY_AGENT", "benchmark-agent")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return completed

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    question = _question(tmp_path)

    assert json.loads(adapter._agy_call(prompt="prompt", question=question)) == {
        "classification": "ambiguous"
    }
    command = captured["cmd"]
    assert command[command.index("--model") + 1] == "gemini-3.6-flash-high"
    assert command[command.index("--agent") + 1] == "benchmark-agent"
    assert "--project" not in command
    add_dirs = [command[i + 1] for i, value in enumerate(command) if value == "--add-dir"]
    assert add_dirs == ["/data/cohort", "/work"]
    assert command[command.index("--output-format") + 1] == "json"
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["required"] == ["classification"]
    assert schema["properties"]["classification"]["type"] == "string"
    assert "--disable-slash-commands" in command
    assert "--dangerously-skip-permissions" not in command
    assert captured["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert str(tmp_path) not in " ".join(command)


def test_antigravity_passes_explicit_effort(monkeypatch, tmp_path, _stub_outer_sandbox):
    completed = subprocess.CompletedProcess(
        args=["agy"], returncode=0, stdout='{"classification":"ambiguous"}', stderr=""
    )
    monkeypatch.setenv("AGY_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("AGY_EFFORT", "low")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    adapter._agy_call(prompt="prompt", question=_question(tmp_path))
    command = captured["cmd"]
    assert command[command.index("--effort") + 1] == "low"


def test_antigravity_unwraps_structured_json_response():
    output = json.dumps({
        "response": {
            "structured_output": {
                "classification": "unambiguous",
                "rationale": "clear",
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 4},
    })
    assert adapter._extract_cli_result(output) == {
        "classification": "unambiguous",
        "rationale": "clear",
    }


def test_antigravity_failure_preserves_both_streams(
    monkeypatch, tmp_path, _stub_outer_sandbox
):
    completed = subprocess.CompletedProcess(
        args=["agy"],
        returncode=1,
        stdout="provider response: model unavailable",
        stderr="request failed with 503",
    )
    monkeypatch.setenv("AGY_MODEL", "gemini-3.6-flash-high")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(adapter.AntigravityInvocationError) as exc_info:
        adapter._agy_call(prompt="prompt", question=_question(tmp_path))

    message = str(exc_info.value)
    assert "agy exited 1" in message
    assert "--- stdout ---" in message
    assert "model unavailable" in message
    assert "--- stderr ---" in message
    assert "503" in message


def test_antigravity_prompt_hides_host_paths(tmp_path):
    prompt = adapter._build_prompt(_question(tmp_path))
    assert "/data/cohort" in prompt
    assert "/data/dictionary/dictionary.xlsx" in prompt
    assert "/work" in prompt
    assert str(tmp_path) not in prompt


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="bwrap unavailable")
def test_antigravity_adapter_cannot_read_sibling_gold_file(tmp_path, monkeypatch):
    question = _question(tmp_path)
    forbidden = tmp_path / "gold-answer.json"
    forbidden.write_text('{"answer": 42}')

    config = tmp_path / "agy-config"
    (config / "cache").mkdir(parents=True)
    (config / "settings.json").write_text(
        json.dumps({"gcp": {"project": "test-project", "location": "us"}})
    )
    (config / "cache/onboarding.json").write_text(
        json.dumps({"enterpriseOnboardingComplete": True})
    )

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_agy = fake_bin_dir / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "log = args[args.index('--log-file') + 1]\n"
        "open(log, 'w').write('fake Antigravity log')\n"
        f"forbidden = {str(forbidden.resolve())!r}\n"
        "answer = {'classification': 'ambiguous', "
        "'rationale': 'visible' if os.path.exists(forbidden) else 'hidden'}\n"
        "print(json.dumps(answer))\n"
    )
    fake_agy.chmod(0o755)
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.setenv("AGY_MODEL", "gemini-3.6-flash-high")
    monkeypatch.setenv("CLINGEN_AGY_CONFIG_DIR", str(config))

    text = adapter._agy_call(prompt="prompt", question=question)

    assert json.loads(text)["rationale"] == "hidden"
    audit_files = list((Path(question["scratch_dir"]).parent / "adapter_audit").glob("*"))
    assert any(path.name.endswith(".cli.log") for path in audit_files)
    assert not (Path(question["scratch_dir"]) / "agy.classify.log").exists()
