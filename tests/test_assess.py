"""Tests for the agent-free assess task: prompt, reply parsing, runs, backends."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clin_genomic_analysis_benchmark import instructions
from clin_genomic_analysis_benchmark.assess import backends, runner
from clin_genomic_analysis_benchmark.assess.prompt import (
    SEPARATOR,
    AssessPrompt,
    build_assess_prompt,
)
from clin_genomic_analysis_benchmark.cohorts import get_cohort
from clin_genomic_analysis_benchmark.components import COMPONENT_IDS
from clin_genomic_analysis_benchmark.questions.schema import (
    PublicCohortQuestionFile,
    PublicQuestion,
)

_VERDICTS = {cid: {"needs_clarification": cid == "outcome", "rationale": "r"}
             for cid in COMPONENT_IDS}


class FakeBackend:
    """Replies from a list, one per call; an Exception entry is raised."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.prompts: list[AssessPrompt] = []
        self.config = backends.BackendConfig(provider="openai-compatible", model="fake")

    def complete(self, prompt: AssessPrompt) -> backends.Completion:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return backends.Completion(text=reply, stop_reason="stop", response_model="fake-1",
                                   usage={"input_tokens": 10, "output_tokens": 5})

    def provenance(self) -> dict:
        return {"provider": "openai-compatible", "model": "fake"}


# ---- rulebook and prompt -----------------------------------------------------

def test_rulebooks_share_the_conventions_and_stay_separate():
    assess = instructions.assess_rulebook()
    analyze = instructions.analyze_rulebook()
    conventions = instructions.CONVENTIONS_PATH.read_text().strip()
    assert assess.startswith("# Instructions for the assess task")
    assert analyze.startswith("# Instructions for the analyze task")
    assert conventions in assess and conventions in analyze
    # Assess is judged without the data; BPC variable mappings stay in analyze.
    for bpc_only in ("stage_dx", "regimen_cancer_level_dataset", "Answer-type schemas"):
        assert bpc_only not in assess
        assert bpc_only in analyze
    assert "Where a gap belongs" not in analyze
    assert set(instructions.rulebook_provenance("assess")) == {
        "ASSESS_INSTRUCTIONS.md", "CONVENTIONS.md"}
    with pytest.raises(ValueError):
        instructions.rulebook_provenance("classify")


def test_prompt_is_the_rulebook_followed_by_the_question():
    prompt = build_assess_prompt(question_text="What is the median OS?\nIn months.",
                                 cohort=get_cohort("nsclc_2.0_public"))
    assert prompt.rulebook == instructions.assess_rulebook()
    assert prompt.text == prompt.rulebook.rstrip("\n") + SEPARATOR + prompt.question
    # Only the cohort's cancer and the question text: no question id, paths, or data.
    assert prompt.question == (
        "# Question to assess\n\n"
        "Cohort: non-small cell lung cancer (NSCLC) (`nsclc_2.0_public`)\n\n"
        "> What is the median OS?\n> In months.\n\n"
        "Reply with only the JSON object described under \"Output\".\n"
    )


# ---- reply parsing -------------------------------------------------------------

def test_parse_reply_accepts_bare_fenced_and_embedded_json():
    for text in (json.dumps(_VERDICTS),
                 f"```json\n{json.dumps(_VERDICTS)}\n```",
                 f"Here is my answer:\n{json.dumps(_VERDICTS)}\nDone."):
        result, errors, failure = runner.parse_reply(text)
        assert (result, errors, failure) == (_VERDICTS, [], None)


def test_parse_reply_takes_the_last_valid_object_and_respects_strings():
    draft = {"denominator": {"needs_clarification": True}}
    tricky = dict(_VERDICTS, covariates={"needs_clarification": False,
                                         "rationale": "uses {braces} and \"quotes\""})
    text = f"Draft: {json.dumps(draft)}\nFinal: {json.dumps(tricky)}"
    result, errors, failure = runner.parse_reply(text)
    assert result == tricky and failure is None


def test_parse_reply_reports_empty_missing_and_invalid_json():
    assert runner.parse_reply("  ")[2] == "empty reply"
    assert runner.parse_reply("All four components are clear.")[2] == "no JSON object in reply"
    result, errors, failure = runner.parse_reply('{"denominator": {"needs_clarification": 1}}')
    assert failure == "schema_violation"
    assert errors and result == {"denominator": {"needs_clarification": 1}}


# ---- calling the model -----------------------------------------------------------

def _prompt() -> AssessPrompt:
    return AssessPrompt(rulebook="RULES", question="QUESTION")


def test_call_model_retries_until_a_reply_validates(tmp_path):
    backend = FakeBackend([RuntimeError("503"), "not json", json.dumps(_VERDICTS)])
    call = runner.call_model(backend=backend, prompt=_prompt(), qdir=tmp_path / "q",
                             max_attempts=3, retry_base_seconds=0)
    assert call.success is True
    assert call.result == _VERDICTS
    assert [a.failure_reason for a in call.attempts] == [
        "backend error: RuntimeError: 503", "no JSON object in reply", None]
    assert call.attempts[2].usage == {"input_tokens": 10, "output_tokens": 5}
    assert json.loads((tmp_path / "q" / "assess.json").read_text()) == _VERDICTS
    assert (tmp_path / "q" / "response.2.txt").read_text() == "not json"


def test_call_model_records_a_truncated_failure(tmp_path):
    class Truncating(FakeBackend):
        def complete(self, prompt):
            return backends.Completion(text='{"denominator": {', stop_reason="max_tokens")

    call = runner.call_model(backend=Truncating([]), prompt=_prompt(), qdir=tmp_path,
                             max_attempts=2, retry_base_seconds=0)
    assert call.success is False and call.result is None
    assert call.attempt_count == 2
    assert call.failure_reason == (
        "no JSON object in reply (reply truncated: stop_reason=max_tokens)")
    assert not (tmp_path / "assess.json").exists()


# ---- whole runs ----------------------------------------------------------------

@pytest.fixture
def bank(monkeypatch, tmp_path):
    public = PublicCohortQuestionFile(
        cohort="bladder_1.2", generated_at="2026-01-01T00:00:00Z", model="m",
        questions=[PublicQuestion(id="bladder_1.2-Q1", category=1, text="How many?"),
                   PublicQuestion(id="bladder_1.2-Q2", category=2, text="How long?")])
    monkeypatch.setattr(runner.q_io, "load_public", lambda name: public)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    return public


def test_run_assess_writes_a_prompt_only_run(bank, tmp_path):
    backend = FakeBackend([json.dumps(_VERDICTS), "no verdict here", "still none"])
    run_dir = runner.run_assess(
        backend_config=backend.config, run_name="assess_fake", cohort_spec="bladder_1.2",
        max_parallel=1, max_attempts=2, retry_base_seconds=0, run_id="r1", backend=backend)

    assert run_dir == tmp_path / "runs" / "assess_fake" / "r1"
    assert all(p.rulebook == instructions.assess_rulebook() for p in backend.prompts)
    assert (run_dir / "rulebook.md").read_text() == instructions.assess_rulebook()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["task"] == "assess"
    assert manifest["integrity"]["boundary"] == "prompt_only"
    assert manifest["integrity"]["status"] == "valid"
    assert manifest["n_questions"] == 2 and manifest["n_completed"] == 1
    assert manifest["model_provenance"] == {"provider": "openai-compatible", "model": "fake"}
    assert manifest["backend_config"]["model"] == "fake"
    assert set(manifest["rulebook"]) == {
        "ASSESS_INSTRUCTIONS.md", "CONVENTIONS.md", "assembled_sha256"}
    runs = json.loads((run_dir / "runs.json").read_text())
    assert [(r["question_id"], r["assess"]["success"]) for r in runs] == [
        ("bladder_1.2-Q1", True), ("bladder_1.2-Q2", False)]
    qdir = run_dir / "per_question" / "bladder_1.2" / "bladder_1.2-Q1"
    assert (qdir / "question.md").read_text().startswith("# Question to assess")
    with pytest.raises(FileExistsError):
        runner.run_assess(backend_config=backend.config, run_name="assess_fake",
                          cohort_spec="bladder_1.2", run_id="r1", backend=backend)


def test_retry_assess_failures_merges_into_a_copy_and_archives_the_source(bank, tmp_path):
    first = FakeBackend([json.dumps(_VERDICTS), "nothing"])
    source = runner.run_assess(
        backend_config=first.config, run_name="assess_fake", cohort_spec="bladder_1.2",
        max_parallel=1, max_attempts=1, retry_base_seconds=0, run_id="r1", backend=first)
    (source / "scorecard.json").write_text("gold-bearing")

    retry = FakeBackend([json.dumps(_VERDICTS)])
    summary = runner.retry_assess_failures(run_path=source, max_parallel=1,
                                           retry_base_seconds=0, output_run_id="r1-retry",
                                           backend=retry)

    assert summary.n_targets == 1 and summary.n_recovered == 1
    assert [p.question for p in retry.prompts] == [first.prompts[1].question]
    retried = summary.retried_run_dir
    assert retried == tmp_path / "runs" / "assess_fake" / "r1-retry"
    assert summary.source_run_dir == tmp_path / "runs" / "assess_fake" / "pre_repair" / "r1"
    assert not source.exists() and summary.source_run_dir.is_dir()
    assert not (retried / "scorecard.json").exists()
    runs = json.loads((retried / "runs.json").read_text())
    assert all(r["assess"]["success"] for r in runs)
    manifest = json.loads((retried / "manifest.json").read_text())
    assert manifest["n_completed"] == 2
    assert manifest["derived_from_run"] == "assess_fake/pre_repair/r1"
    assert manifest["retry_history"][0]["targets"][0]["merged"] is True
    with pytest.raises(ValueError, match="No failed assess calls"):
        runner.retry_assess_failures(run_path=retried, backend=retry)


def test_retry_refuses_after_the_rulebook_changes(bank, tmp_path, monkeypatch):
    backend = FakeBackend(["nothing", "nothing"])
    source = runner.run_assess(
        backend_config=backend.config, run_name="assess_fake", cohort_spec="bladder_1.2",
        max_parallel=1, max_attempts=1, retry_base_seconds=0, run_id="r1", backend=backend)
    monkeypatch.setattr(runner, "assess_rulebook", lambda: "a different rulebook")
    with pytest.raises(ValueError, match="rulebook or conventions changed"):
        runner.retry_assess_failures(run_path=source, backend=FakeBackend([]))


# ---- backends --------------------------------------------------------------------

def test_model_spec_parsing():
    assert backends.parse_model_spec("vertex-openai:google/gemini-3-pro") == (
        "vertex-openai", "google/gemini-3-pro")
    assert backends.parse_model_spec("openai-compatible:qwen:35b") == (
        "openai-compatible", "qwen:35b")
    for bad in ("claude-opus-5-5", "made-up:model", "azure-openai:"):
        with pytest.raises(ValueError):
            backends.parse_model_spec(bad)


def test_default_run_name_is_filesystem_safe():
    assert runner.default_run_name("vertex-openai:google/gemini 3") == (
        "assess_vertex-openai-google-gemini-3")


def test_backend_config_round_trips_through_the_manifest():
    config = backends.BackendConfig(provider="azure-openai", model="gpt-5", max_tokens=100,
                                    params={"reasoning_effort": "high"})
    assert backends.BackendConfig.from_dict({**config.to_dict(), "unknown": 1}) == config


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get_final_message(self):
        return self.message


def test_vertex_anthropic_caches_the_rulebook_and_skips_thinking(monkeypatch):
    captured = {}
    message = SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="hmm"),
                 SimpleNamespace(type="text", text='{"a": 1}')],
        stop_reason="end_turn", model="claude-x",
        usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=4,
                              cache_creation_input_tokens=0))

    class FakeVertex:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = SimpleNamespace(
                stream=lambda **kw: captured.setdefault("request", kw) and _FakeStream(message))

    import anthropic
    monkeypatch.setattr(anthropic, "AnthropicVertex", FakeVertex)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_REGION", raising=False)
    backend = backends.make_backend(backends.BackendConfig(
        provider="vertex-anthropic", model="claude-x",
        params={"thinking": {"type": "adaptive"}}))

    completion = backend.complete(AssessPrompt(rulebook="RULES\n", question="Q"))

    request = captured["request"]
    blocks = request["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "RULES" + SEPARATOR,
                         "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": "Q"}
    assert request["max_tokens"] == backends.DEFAULT_ANTHROPIC_MAX_TOKENS
    assert request["thinking"] == {"type": "adaptive"}
    assert captured["client"]["max_retries"] == 0
    assert completion.text == '{"a": 1}'
    assert completion.usage["cache_read_input_tokens"] == 4
    assert backend.provenance()["project"] == "proj"
    assert backend.provenance()["region"] == "global"


def test_openai_compatible_sends_one_user_message_and_redacts_the_url(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured["request"] = kwargs
        return SimpleNamespace(
            model="served-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="reply"),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("MY_KEY", "secret-key")
    backend = backends.make_backend(backends.BackendConfig(
        provider="openai-compatible", model="gemma", max_tokens=64, temperature=0.0,
        base_url="http://user:pw@127.0.0.1:8000/v1?token=abc", api_key_env="MY_KEY"))

    completion = backend.complete(AssessPrompt(rulebook="RULES", question="Q"))

    assert captured["client"]["api_key"] == "secret-key"
    assert captured["request"]["messages"] == [
        {"role": "user", "content": "RULES" + SEPARATOR + "Q"}]
    assert captured["request"]["max_tokens"] == 64
    assert captured["request"]["temperature"] == 0.0
    assert (completion.text, completion.stop_reason, completion.response_model) == (
        "reply", "stop", "served-model")
    provenance = backend.provenance()
    assert provenance["base_url"] == "http://127.0.0.1:8000/v1"
    assert "secret-key" not in json.dumps(provenance)


def test_vertex_responses_calls_the_publisher_endpoint_with_the_bare_model(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured["request"] = kwargs
        return SimpleNamespace(
            model="meta/muse-ember-1.0", output_text="reply", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(input_tokens=7, output_tokens=3))

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = SimpleNamespace(create=create)

    class FakeCredentials:
        valid, token = True, "google-token"

    import google.auth
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(google.auth, "default", lambda scopes: (FakeCredentials(), "proj"))
    backend = backends.make_backend(backends.BackendConfig(
        provider="vertex-responses", model="meta/muse-ember-1.0", max_tokens=64,
        project="proj"))

    completion = backend.complete(AssessPrompt(rulebook="RULES", question="Q"))

    assert captured["client"]["base_url"] == (
        "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/publishers/meta/v1")
    assert captured["client"]["api_key"] == "google-token"
    assert captured["request"] == {"model": "muse-ember-1.0", "input": "RULES" + SEPARATOR + "Q",
                                   "max_output_tokens": 64}
    assert (completion.text, completion.stop_reason) == ("reply", "length")
    assert completion.usage == {"input_tokens": 7, "output_tokens": 3}
    with pytest.raises(ValueError, match="<publisher>/<model>"):
        backends.make_backend(backends.BackendConfig(
            provider="vertex-responses", model="muse-ember-1.0", project="proj"))


def test_openai_compatible_needs_a_base_url_and_a_set_key(monkeypatch):
    monkeypatch.delenv("CLINGEN_ASSESS_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="--base-url"):
        backends.make_backend(backends.BackendConfig(provider="openai-compatible", model="m"))
    monkeypatch.delenv("UNSET_KEY", raising=False)
    with pytest.raises(ValueError, match="UNSET_KEY"):
        backends.make_backend(backends.BackendConfig(
            provider="openai-compatible", model="m", base_url="http://x/v1",
            api_key_env="UNSET_KEY"))
