"""Tests for scoring assess and analyze runs separately, and combining them."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from clin_genomic_analysis_benchmark.components import COMPONENT_IDS
from clin_genomic_analysis_benchmark.questions.schema import (
    CohortQuestionFile,
    ComponentAssessment,
    Question,
)
from clin_genomic_analysis_benchmark.scoring import combine, driver
from clin_genomic_analysis_benchmark.scoring.aggregator import QuestionScore, aggregate_assess
from clin_genomic_analysis_benchmark.scoring.assessment import score_assessment

_RULEBOOK = {"ASSESS_INSTRUCTIONS.md": "a", "ANALYZE_INSTRUCTIONS.md": "b",
             "CONVENTIONS.md": "c"}


def _flags(*unclear: str) -> dict[str, bool]:
    return {cid: cid in unclear for cid in COMPONENT_IDS}


def _assess_result(*unclear: str) -> dict:
    return {cid: {"needs_clarification": cid in unclear, "rationale": f"{cid} note"}
            for cid in COMPONENT_IDS}


def _components(*unclear: str) -> ComponentAssessment:
    return ComponentAssessment(**{
        cid: {"needs_clarification": cid in unclear} for cid in COMPONENT_IDS})


def _write_run(run_dir, *, task: str, records: list[dict], provenance: dict | None = None,
               integrity: dict | None = None, settings: dict | None = None) -> None:
    run_dir.mkdir(parents=True)
    manifest = {
        "task": task,
        "run_id": run_dir.name,
        "cohorts": ["cohort_1"],
        "integrity": integrity or {"status": "valid"},
        "rulebook": _RULEBOOK,
        "settings": settings or {},
    }
    if task == "assess":
        manifest.update(run_name="assess_model", model_provenance=provenance or {})
    elif task == "analyze":
        manifest.update(agent_name="agent", agent_provenance=provenance or {})
    else:
        del manifest["task"]
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "runs.json").write_text(json.dumps(records))


def _assess_record(assess: dict, qid: str = "cohort_1-Q1") -> dict:
    return {"cohort": "cohort_1", "question_id": qid, "category": 1, "assess": assess,
            "error": None}


def _analyze_record(result: dict | None, qid: str = "cohort_1-Q1", **failure) -> dict:
    analyze = ({"success": True, "result": result} if result is not None
               else {"success": False, **failure})
    return {"cohort": "cohort_1", "question_id": qid, "category": 8, "analyze": analyze,
            "error": None}


def _gold_bank(*questions: Question) -> CohortQuestionFile:
    return CohortQuestionFile(
        cohort="cohort_1", generated_at=datetime.now(timezone.utc), model="test",
        questions=list(questions))


def _ambiguous(*unclear: str, qid: str = "cohort_1-Q1") -> Question:
    return Question(
        id=qid, category=1, text="How well did treatment work?",
        classification="ambiguous", component_assessment=_components(*unclear))


def _unambiguous(*, answer_type: str, gold_answer: dict | None,
                 qid: str = "cohort_1-Q1") -> Question:
    return Question(
        id=qid,
        category=8,
        text="Is there a significant treatment-by-biomarker interaction?",
        classification="unambiguous",
        component_assessment=_components(),
        analysis_spec={
            "population_unit": "patient",
            "tables": ["data_clinical_patient.txt"],
            "statistic": "Cox interaction",
            "expected_answer_type": answer_type,
        },
        gold_answer=gold_answer,
    )


def _scorecard(run_dir) -> dict:
    return json.loads((run_dir / "scorecard.json").read_text())


# ---- assess runs -------------------------------------------------------------

def test_assess_run_scores_each_component_exactly(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "assess_model" / "run-1"
    _write_run(
        run_dir, task="assess",
        records=[_assess_record({"success": True,
                                 "result": _assess_result("outcome", "covariates")})],
        provenance={"provider": "vertex-anthropic", "model": "claude-opus-5-5",
                    "params": {"thinking": {"type": "adaptive"}}},
        integrity={"status": "valid", "boundary": "prompt_only"},
    )
    monkeypatch.setattr(driver.q_io, "load_gold",
                        lambda cohort: _gold_bank(_ambiguous("outcome", "analysis_plan")))

    driver.score_run(run_path=str(run_dir))

    card = _scorecard(run_dir)
    md = (run_dir / "scorecard.md").read_text()
    assert card["scorer"] == "deterministic-rules-v4"
    assert card["task"] == "assess"
    assert card["provenance"]["model"] == "claude-opus-5-5"
    assert card["rulebook"] == _RULEBOOK
    assert "- **Integrity**: `valid` (prompt only: no tools, no data access)" in md
    assert '- **Request parameters**: `{"thinking": {"type": "adaptive"}}`' in md
    assessment = card["questions"][0]["assessment"]
    assert {c["component"]: c["outcome"] for c in assessment["components"]} == {
        "denominator": "tn", "outcome": "tp", "covariates": "fp", "analysis_plan": "fn"}
    assert assessment["n_correct"] == 2
    assert assessment["classification_correct"] is True
    assert assessment["agent_rationales"]["outcome"] == "outcome note"
    components = card["overall"]["components"]
    assert components["outcome"]["sensitivity"] == 1.0
    assert components["analysis_plan"]["sensitivity"] == 0.0
    assert components["covariates"]["specificity"] == 0.0
    assert "analysis" not in card["overall"]
    assert "| `cohort_1-Q1` | cohort_1 | 1 | O+P | O+C | 2/4 |" in md


def test_failed_assess_counts_every_component_wrong(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "assess_model" / "run-1"
    _write_run(run_dir, task="assess", records=[_assess_record(
        {"success": False, "failure_reason": "no JSON object in reply"})])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(_ambiguous("outcome")))

    driver.score_run(run_path=str(run_dir))

    question = _scorecard(run_dir)["questions"][0]
    assert question["failure_reason"] == "no JSON object in reply"
    assert question["assessment"]["agent_classification"] is None
    assert [c["outcome"] for c in question["assessment"]["components"]] == [
        "fp", "fn", "fp", "fp"]
    assert _scorecard(run_dir)["overall"]["components"]["outcome"]["n_missing"] == 1
    assert "| `cohort_1-Q1` | cohort_1 | 1 | O | failed | 0/4 |" in (
        run_dir / "scorecard.md").read_text()


def test_assess_run_lists_gold_questions_it_missed(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "assess_model" / "run-1"
    _write_run(run_dir, task="assess", records=[_assess_record(
        {"success": True, "result": _assess_result()})])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(
        _ambiguous("outcome"), _ambiguous("outcome", qid="cohort_1-Q2")))

    driver.score_run(run_path=str(run_dir))

    card = _scorecard(run_dir)
    assert card["coverage"]["missing_from_run"] == [
        {"cohort": "cohort_1", "question_id": "cohort_1-Q2"}]
    assert card["overall"]["n"] == 1


def test_driver_refuses_quarantined_run(tmp_path):
    run_dir = tmp_path / "runs" / "agent" / "quarantined"
    _write_run(run_dir, task="analyze", records=[], integrity={"status": "quarantined"})
    with pytest.raises(ValueError, match="refusing to score a quarantined run"):
        driver.score_run(run_path=str(run_dir))


def test_driver_refuses_a_run_from_before_the_split(tmp_path):
    run_dir = tmp_path / "runs" / "agent" / "old"
    _write_run(run_dir, task="legacy", records=[])
    with pytest.raises(ValueError, match="before the assess/analyze split"):
        driver.score_run(run_path=str(run_dir))


def test_driver_refuses_gold_without_component_review(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "assess_model" / "run-1"
    _write_run(run_dir, task="assess", records=[_assess_record(
        {"success": True, "result": _assess_result()})])
    unreviewed = _gold_bank(Question(
        id="cohort_1-Q1", category=1, text="Legacy question", classification="ambiguous",
        disambiguation_concept_ids=["OUTCOME_METRIC"]))
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: unreviewed)
    with pytest.raises(ValueError, match="no component_assessment"):
        driver.score_run(run_path=str(run_dir))


def test_driver_rejects_retired_disambiguation_config(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "assess_model" / "run-1"
    _write_run(run_dir, task="assess", records=[_assess_record(
        {"success": True, "result": _assess_result()})])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(_ambiguous("outcome")))
    config = tmp_path / "scoring.yaml"
    config.write_text("disambiguation:\n  correct_concept_points: 1.0\n")
    with pytest.raises(ValueError, match="concept-ID scoring was retired"):
        driver.score_run(run_path=str(run_dir), scoring_config_path=config)


# ---- analyze runs ------------------------------------------------------------

def test_analyze_run_scores_points_over_gold_unambiguous(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, task="analyze", records=[
        _analyze_record({"answer_type": "count", "answer": {"value": 10, "n": 10}}),
        _analyze_record(None, qid="cohort_1-Q2", failure_reason="timeout"),
    ], provenance={"model": "claude-opus-5-5", "effort_level": "xhigh",
                   "effort_supported": True})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(
        _unambiguous(answer_type="count", gold_answer={"value": 10}),
        _unambiguous(answer_type="count", gold_answer={"value": 5}, qid="cohort_1-Q2"),
        _ambiguous("outcome", qid="cohort_1-Q3"),
    ))

    driver.score_run(run_path=str(run_dir))

    card = _scorecard(run_dir)
    md = (run_dir / "scorecard.md").read_text()
    assert card["task"] == "analyze"
    assert card["overall"]["points"] == 2
    assert card["overall"]["possible"] == 4
    assert card["overall"]["score"] == 0.5
    assert card["overall"]["n_failed"] == 1
    assert card["coverage"] == {"missing_from_run": [], "excluded": []}
    assert "components" not in card["overall"]
    assert "- **Effort**: `xhigh`" in md
    assert "| `cohort_1-Q2` | cohort_1 | 8 | count | 0 — timeout |" in md


def test_analyze_run_excludes_questions_whose_gold_changed(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, task="analyze", records=[
        _analyze_record({"answer_type": "count", "answer": {"value": 10, "n": 10}})])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(
        _ambiguous("outcome"),
        _unambiguous(answer_type="count", gold_answer={"value": 5}, qid="cohort_1-Q2")))

    driver.score_run(run_path=str(run_dir))

    card = _scorecard(run_dir)
    assert card["overall"]["n"] == 0
    assert card["overall"]["score"] is None
    assert card["coverage"]["excluded"] == [{
        "cohort": "cohort_1", "question_id": "cohort_1-Q1",
        "reason": "gold is no longer unambiguous"}]
    assert card["coverage"]["missing_from_run"] == [
        {"cohort": "cohort_1", "question_id": "cohort_1-Q2"}]


def test_question_filter_does_not_report_missing_questions(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, task="analyze", settings={"question_filter": "cohort_1-Q1"},
               records=[_analyze_record({"answer_type": "count", "answer": {"value": 10}})])
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(
        _unambiguous(answer_type="count", gold_answer={"value": 10}),
        _unambiguous(answer_type="count", gold_answer={"value": 5}, qid="cohort_1-Q2")))

    driver.score_run(run_path=str(run_dir))

    assert _scorecard(run_dir)["coverage"]["missing_from_run"] == []


@pytest.mark.parametrize(
    "agent_answer, expected_band, expected_points, expected_metric",
    [
        ({"value": 1.0, "unanswerable": True, "unanswerable_reason": "separation"},
         "accurate", 2, "unanswerable_match"),
        ({"value": 0.83, "test_name": "Wald", "n_total": 376},
         "major", 0, "unanswerable_mismatch"),
    ],
    ids=["flagged", "not_flagged"],
)
def test_unanswerable_gold_skips_answer_type_check(
        tmp_path, monkeypatch, agent_answer, expected_band, expected_points, expected_metric):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, task="analyze", records=[
        _analyze_record({"answer_type": "pvalue", "answer": agent_answer})])
    gold = _unambiguous(answer_type="categorical",
                        gold_answer={"value": "Not estimable: separation", "unanswerable": True})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(gold))

    driver.score_run(run_path=str(run_dir))

    question = _scorecard(run_dir)["questions"][0]
    assert question["analysis"]["band"] == expected_band
    assert question["analysis"]["points"] == expected_points
    assert question["analysis"]["metric"] == expected_metric


def test_answerable_gold_keeps_answer_type_check(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, task="analyze", records=[_analyze_record({
        "answer_type": "pvalue",
        "answer": {"value": 0.04, "test_name": "Wald", "n_total": 100,
                   "unanswerable": True, "unanswerable_reason": "separation"},
    })])
    gold = _unambiguous(answer_type="hazard_ratio_with_ci",
                        gold_answer={"value": 1.5, "ci_low": 1.1, "ci_high": 2.0,
                                     "p_value": 0.04})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _gold_bank(gold))

    driver.score_run(run_path=str(run_dir))

    question = _scorecard(run_dir)["questions"][0]
    assert question["analysis"]["band"] == "major"
    assert question["analysis"]["points"] == 0
    assert question["analysis"]["metric"] == "answer_type_mismatch"


# ---- aggregation ---------------------------------------------------------------

def _scored(qid: str, gold: dict[str, bool], agent: dict[str, bool] | None) -> QuestionScore:
    assessment = score_assessment(question_id=qid, cohort="c", gold_flags=gold,
                                  agent_flags=agent)
    return QuestionScore(question_id=qid, cohort="c", category=1,
                         gold_classification=assessment.gold_classification,
                         assessment=assessment)


def test_always_clear_model_scores_half_on_assess():
    """Balanced accuracy, not plain accuracy: answering "clear" everywhere is 50%."""
    qs = [_scored("u", _flags(*COMPONENT_IDS), _flags())]
    qs += [_scored(f"c{i}", _flags(), _flags()) for i in range(3)]
    overall, _ = aggregate_assess(qs)
    for cid in COMPONENT_IDS:
        tally = overall.components[cid]
        assert tally.accuracy == 0.75
        assert tally.balanced_accuracy == 0.5
    assert overall.score == 0.5


def test_single_class_component_uses_the_defined_rate():
    qs = [_scored("a", _flags("outcome"), _flags("outcome", "covariates")),
          _scored("b", _flags(), _flags())]
    overall, _ = aggregate_assess(qs)
    covariates = overall.components["covariates"]   # gold never unclear
    assert covariates.sensitivity is None
    assert covariates.specificity == 0.5
    assert covariates.single_class is True
    assert covariates.balanced_accuracy == 0.5
    assert overall.components["outcome"].balanced_accuracy == 1.0


# ---- combining ---------------------------------------------------------------

@pytest.fixture
def scored_pair(tmp_path, monkeypatch):
    """A scored assess run (score 0.75 on Q1) and analyze run (score 0.5)."""
    gold = _gold_bank(_unambiguous(answer_type="count", gold_answer={"value": 10}),
                      _unambiguous(answer_type="count", gold_answer={"value": 5},
                                   qid="cohort_1-Q2"))
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: gold)
    monkeypatch.setattr(combine, "RUNS_DIR", tmp_path / "runs")
    assess_dir = tmp_path / "runs" / "assess_model" / "run-a"
    _write_run(assess_dir, task="assess", provenance={"model": "m-assess"}, records=[
        _assess_record({"success": True, "result": _assess_result()}),
        _assess_record({"success": True, "result": _assess_result("outcome")},
                       qid="cohort_1-Q2"),
    ])
    analyze_dir = tmp_path / "runs" / "agent" / "run-b"
    _write_run(analyze_dir, task="analyze", provenance={"model": "m-analyze"}, records=[
        _analyze_record({"answer_type": "count", "answer": {"value": 10}}),
        _analyze_record(None, qid="cohort_1-Q2", failure_reason="timeout"),
    ])
    driver.score_run(run_path=str(assess_dir))
    driver.score_run(run_path=str(analyze_dir))
    return assess_dir, analyze_dir


def test_combine_weights_the_two_tasks(scored_pair):
    assess_dir, analyze_dir = scored_pair
    assess_score = _scorecard(assess_dir)["overall"]["score"]
    assert _scorecard(analyze_dir)["overall"]["score"] == 0.5

    out = combine.combine_runs(assess_run=assess_dir, analyze_run=analyze_dir, run_id="c1")

    card = _scorecard(out)
    assert out.name == "c1" and out.parent.name == "combined"
    assert card["task"] == "combined"
    assert card["overall"]["subtask_scores"] == {"assess": assess_score, "analyze": 0.5}
    assert abs(card["overall"]["score"] - (0.5 * assess_score + 0.25)) < 1e-9
    assert card["per_cohort"]["cohort_1"]["score"] == card["overall"]["score"]
    assert card["sources"]["assess"]["provenance"]["model"] == "m-assess"
    assert card["sources"]["analyze"]["run"] == "agent/run-b"
    assert "**SCORE:" in (out / "scorecard.md").read_text()


def test_combine_uses_configured_weights(scored_pair, tmp_path):
    assess_dir, analyze_dir = scored_pair
    config = tmp_path / "weights.yaml"
    config.write_text("subtask_weights:\n  assess: 0.0\n  analyze: 1.0\n")
    out = combine.combine_runs(assess_run=assess_dir, analyze_run=analyze_dir,
                               scoring_config_path=config)
    assert _scorecard(out)["overall"]["score"] == 0.5


def test_combine_refuses_different_gold_or_conventions(scored_pair):
    assess_dir, analyze_dir = scored_pair
    card = _scorecard(analyze_dir)
    card["gold_bank_sha256"] = "other"
    card["rulebook"]["CONVENTIONS.md"] = "other"
    (analyze_dir / "scorecard.json").write_text(json.dumps(card))

    with pytest.raises(ValueError, match="different gold banks.*different versions"):
        combine.combine_runs(assess_run=assess_dir, analyze_run=analyze_dir)
    out = combine.combine_runs(assess_run=assess_dir, analyze_run=analyze_dir,
                               allow_mismatch=True)
    assert len(_scorecard(out)["mismatches_allowed"]) == 2


def test_combine_needs_one_run_of_each_task(scored_pair):
    assess_dir, _ = scored_pair
    with pytest.raises(ValueError, match="not a deterministic-rules-v4 analyze scorecard"):
        combine.combine_runs(assess_run=assess_dir, analyze_run=assess_dir)


def test_subtask_weights_are_validated():
    assert combine.validate_subtask_weights({"assess": 0.8, "analyze": 0.2}) == {
        "assess": 0.8, "analyze": 0.2}
    with pytest.raises(ValueError, match="unknown subtask weight"):
        combine.validate_subtask_weights({"classify": 0.5, "disambiguate": 0.5})
    # A task with nothing to score drops out and the other carries the headline.
    assert combine.weighted_score({"assess": 0.6, "analyze": None},
                                  {"assess": 0.5, "analyze": 0.5}) == 0.6
