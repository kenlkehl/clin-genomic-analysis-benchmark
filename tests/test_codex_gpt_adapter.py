"""Focused tests for explicit Codex CLI runtime selection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1] / "adapters" / "codex_gpt" / "adapter.py"
)
_SPEC = importlib.util.spec_from_file_location("codex_gpt_adapter", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)


def test_codex_call_passes_explicit_provider_model_and_effort(tmp_path, monkeypatch):
    captured = {}
    scratch_dir = tmp_path / "scratch"
    cohort_dir = tmp_path / "cohort"
    cohort_dir.mkdir()
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    completed = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout='{"classification":"ambiguous"}', stderr=""
    )
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CODEX_MODEL_PROVIDER", "azure")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "xhigh")
    monkeypatch.delenv("CODEX_PROFILE", raising=False)
    monkeypatch.setattr(adapter, "_refresh_azure_token", lambda env: None)

    @contextmanager
    def fake_sandbox(cmd, **kwargs):
        yield SimpleNamespace(command=cmd, environment=kwargs["environment"])

    monkeypatch.setattr(adapter, "sandboxed_agent_command", fake_sandbox)
    monkeypatch.setattr(
        adapter, "export_agent_session_audit", lambda *args, **kwargs: []
    )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result = adapter._codex_call(
        prompt="prompt",
        question={
            "stage": "classify",
            "scratch_dir": str(scratch_dir),
            "cohort_dir": str(cohort_dir),
            "data_dictionary_path": str(dictionary),
        },
        last_message_file=scratch_dir / "last-message.txt",
    )

    assert result == completed.stdout
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_provider="azure"' in captured["cmd"]
    assert 'model_reasoning_effort="xhigh"' in captured["cmd"]


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="bwrap unavailable")
def test_codex_adapter_cannot_read_sibling_gold_file(tmp_path, monkeypatch):
    cohort = tmp_path / "cohort"
    scratch = tmp_path / "question" / "scratch"
    cohort.mkdir()
    scratch.mkdir(parents=True)
    dictionary = tmp_path / "dictionary.xlsx"
    dictionary.write_text("dictionary")
    forbidden = tmp_path / "gold-answer.json"
    forbidden.write_text('{"answer": 42}')

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_codex = fake_bin_dir / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"forbidden = {str(forbidden.resolve())!r}\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "result = {'classification': 'ambiguous', "
        "'rationale': 'visible' if os.path.exists(forbidden) else 'hidden'}\n"
        "open(out, 'w').write(json.dumps(result))\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("CODEX_REFRESH_AZURE_TOKEN", "0")
    monkeypatch.delenv("CODEX_PROFILE", raising=False)
    monkeypatch.delenv("CODEX_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    text = adapter._codex_call(
        prompt="prompt",
        question={
            "stage": "classify",
            "scratch_dir": str(scratch),
            "cohort_dir": str(cohort),
            "data_dictionary_path": str(dictionary),
        },
        last_message_file=scratch / "last-message.txt",
    )

    assert json.loads(text)["rationale"] == "hidden"
