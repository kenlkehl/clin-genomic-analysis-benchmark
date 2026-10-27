"""Tests for the score-run driver and the assess/analyze aggregation."""

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
from clin_genomic_analysis_benchmark.scoring import driver
from clin_genomic_analysis_benchmark.scoring.aggregator import QuestionScore, aggregate
from clin_genomic_analysis_benchmark.scoring.assessment import score_assessment


def _flags(*unclear: str) -> dict[str, bool]:
    return {cid: cid in unclear for cid in COMPONENT_IDS}


def _assess_result(*unclear: str) -> dict:
    return {cid: {"needs_clarification": cid in unclear, "rationale": f"{cid} note"}
            for cid in COMPONENT_IDS}


def _components(*unclear: str) -> ComponentAssessment:
    return ComponentAssessment(**{
        cid: {"needs_clarification": cid in unclear} for cid in COMPONENT_IDS})


def _write_run(run_dir, *, assess: dict | None, analyze_result: dict | None = None,
               agent_provenance: dict | None = None,
               integrity: dict | None = None) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "agent_name": "agent",
        "run_id": "run-1",
        "cohorts": ["cohort_1"],
        "agent_provenance": agent_provenance or {},
        "integrity": integrity or {"status": "unaudited"},
    }))
    qrun = {
        "cohort": "cohort_1",
        "question_id": "cohort_1-Q1",
        "category": 1,
        "assess": assess,
    }
    if analyze_result is not None:
        qrun["analyze"] = {"success": True, "result": analyze_result}
    (run_dir / "runs.json").write_text(json.dumps([qrun]))


def _gold_bank(question: Question) -> CohortQuestionFile:
    return CohortQuestionFile(
        cohort="cohort_1", generated_at=datetime.now(timezone.utc), model="test",
        questions=[question])


def _ambiguous_gold(*unclear: str) -> CohortQuestionFile:
    return _gold_bank(Question(
        id="cohort_1-Q1", category=1, text="How well did treatment work?",
        classification="ambiguous", component_assessment=_components(*unclear)))


def _unambiguous_gold(*, answer_type: str, gold_answer: dict) -> CohortQuestionFile:
    return _gold_bank(Question(
        id="cohort_1-Q1",
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
    ))


def test_driver_scores_each_component_exactly(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(
        run_dir,
        assess={"success": True, "result": _assess_result("outcome", "covariates")},
        agent_provenance={
            "model": "claude-sonnet-5@20260203",
            "provider": "google_vertex_ai",
            "effort_level": "xhigh",
            "effort_supported": True,
            "effort_source": "CLINGEN_CLAUDE_EFFORT",
        },
        integrity={"status": "valid", "sandbox": {"backend": "bubblewrap"}},
    )
    monkeypatch.setattr(driver.q_io, "load_gold",
                        lambda cohort: _ambiguous_gold("outcome", "analysis_plan"))

    driver.score_run(run_path=str(run_dir))

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    md = (run_dir / "scorecard.md").read_text()
    assert scorecard["scorer"] == "deterministic-rules-v3"
    assert scorecard["agent_provenance"]["effort_level"] == "xhigh"
    assert scorecard["integrity"]["status"] == "valid"
    assert "- **Effort**: `xhigh`" in md
    assert "- **Integrity**: `valid`" in md
    assessment = scorecard["questions"][0]["assessment"]
    assert {c["component"]: c["outcome"] for c in assessment["components"]} == {
        "denominator": "tn", "outcome": "tp", "covariates": "fp", "analysis_plan": "fn"}
    assert assessment["n_correct"] == 2
    assert assessment["agent_classification"] == "ambiguous"
    assert assessment["classification_correct"] is True
    assert assessment["agent_rationales"]["outcome"] == "outcome note"
    components = scorecard["overall"]["components"]
    assert components["outcome"]["sensitivity"] == 1.0
    assert components["analysis_plan"]["sensitivity"] == 0.0
    assert components["covariates"]["specificity"] == 0.0
    assert "| `cohort_1-Q1` | cohort_1 | 1 | O+P | O+C | 2/4 |" in md


def test_driver_refuses_quarantined_run(tmp_path):
    run_dir = tmp_path / "runs" / "agent" / "quarantined"
    _write_run(run_dir, assess={"success": True, "result": _assess_result()},
               integrity={"status": "quarantined"})

    with pytest.raises(ValueError, match="refusing to score a quarantined run"):
        driver.score_run(run_path=str(run_dir))


def test_driver_refuses_gold_without_component_review(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, assess={"success": True, "result": _assess_result()})
    unreviewed = _gold_bank(Question(
        id="cohort_1-Q1", category=1, text="Legacy question", classification="ambiguous",
        disambiguation_concept_ids=["OUTCOME_METRIC"]))
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: unreviewed)

    with pytest.raises(ValueError, match="no component_assessment"):
        driver.score_run(run_path=str(run_dir))


def test_driver_rejects_retired_disambiguation_config(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, assess={"success": True, "result": _assess_result()})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _ambiguous_gold("outcome"))
    config = tmp_path / "scoring.yaml"
    config.write_text("disambiguation:\n  correct_concept_points: 1.0\n")

    with pytest.raises(ValueError, match="concept-ID scoring was retired"):
        driver.score_run(run_path=str(run_dir), scoring_config_path=config)


def test_failed_assess_counts_every_component_wrong(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, assess={"success": False, "failure_reason": "timeout"})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _ambiguous_gold("outcome"))

    driver.score_run(run_path=str(run_dir))

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    question = scorecard["questions"][0]
    assert question["failure_reason"] == "timeout"
    assert question["assessment"]["agent_classification"] is None
    assert [c["outcome"] for c in question["assessment"]["components"]] == [
        "fp", "fn", "fp", "fp"]
    assert scorecard["overall"]["components"]["outcome"]["n_missing"] == 1
    assert "| `cohort_1-Q1` | cohort_1 | 1 | O | failed | 0/4 |" in (
        run_dir / "scorecard.md").read_text()


def test_ambiguous_gold_all_clear_agent_does_not_claim_missing_gold(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(
        run_dir,
        assess={"success": True, "result": _assess_result()},
        analyze_result={"answer_type": "count", "answer": {"value": 10}},
    )
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _ambiguous_gold("outcome"))

    driver.score_run(run_path=str(run_dir))

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    assert scorecard["questions"][0]["failure_reason"] == (
        "gold question is ambiguous; agent found every component clear, "
        "so analysis was not scored"
    )
    assert scorecard["questions"][0]["analysis"] is None
    # A gold-ambiguous question carries no analysis points.
    assert scorecard["overall"]["analysis_possible"] == 0
    assert scorecard["overall"]["subtask_scores"]["analyze"] is None
    assert "compute-gold" not in (run_dir / "scorecard.md").read_text()


def test_flagged_unambiguous_gold_still_owes_analysis_points(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(run_dir, assess={"success": True, "result": _assess_result("denominator")})
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: _unambiguous_gold(
        answer_type="count", gold_answer={"value": 10}))

    driver.score_run(run_path=str(run_dir))

    scorecard = json.loads((run_dir / "scorecard.json").read_text())
    assert scorecard["questions"][0]["failure_reason"].startswith(
        "agent flagged a component of a gold-unambiguous question")
    assert scorecard["overall"]["analysis_points"] == 0
    assert scorecard["overall"]["analysis_possible"] == 2
    assert scorecard["overall"]["subtask_scores"]["analyze"] == 0.0


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
    _write_run(
        run_dir,
        assess={"success": True, "result": _assess_result()},
        analyze_result={"answer_type": "pvalue", "answer": agent_answer},
    )
    gold = _unambiguous_gold(
        answer_type="categorical",
        gold_answer={"value": "Not estimable: separation", "unanswerable": True},
    )
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: gold)

    driver.score_run(run_path=str(run_dir))

    question = json.loads((run_dir / "scorecard.json").read_text())["questions"][0]
    assert question["analysis"]["band"] == expected_band
    assert question["analysis"]["points"] == expected_points
    assert question["analysis"]["metric"] == expected_metric


def test_answerable_gold_keeps_answer_type_check(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "agent" / "run-1"
    _write_run(
        run_dir,
        assess={"success": True, "result": _assess_result()},
        analyze_result={
            "answer_type": "pvalue",
            "answer": {"value": 0.04, "test_name": "Wald", "n_total": 100,
                       "unanswerable": True, "unanswerable_reason": "separation"},
        },
    )
    gold = _unambiguous_gold(
        answer_type="hazard_ratio_with_ci",
        gold_answer={"value": 1.5, "ci_low": 1.1, "ci_high": 2.0, "p_value": 0.04},
    )
    monkeypatch.setattr(driver.q_io, "load_gold", lambda cohort: gold)

    driver.score_run(run_path=str(run_dir))

    question = json.loads((run_dir / "scorecard.json").read_text())["questions"][0]
    assert question["analysis"]["band"] == "major"
    assert question["analysis"]["points"] == 0
    assert question["analysis"]["metric"] == "answer_type_mismatch"


def _scored(qid: str, gold: dict[str, bool], agent: dict[str, bool] | None,
            gold_classification: str | None = None) -> QuestionScore:
    assessment = score_assessment(question_id=qid, cohort="c", gold_flags=gold,
                                  agent_flags=agent)
    return QuestionScore(
        question_id=qid, cohort="c", category=1,
        gold_classification=gold_classification or assessment.gold_classification,
        assessment=assessment,
    )


def test_always_clear_agent_scores_half_on_assess():
    """Balanced accuracy, not plain accuracy: answering "clear" everywhere is 50%."""
    # Gold: one question unclear on every component, three clear on every one.
    qs = [_scored("u", _flags(*COMPONENT_IDS), _flags())]
    qs += [_scored(f"c{i}", _flags(), _flags()) for i in range(3)]
    overall, _ = aggregate(qs)

    for cid in COMPONENT_IDS:
        tally = overall.components[cid]
        assert tally.accuracy == 0.75
        assert tally.balanced_accuracy == 0.5
    assert overall.subtask_scores["assess"] == 0.5
    # Three gold-unambiguous questions, none analysed.
    assert overall.subtask_scores["analyze"] == 0.0
    assert abs(overall.overall_score - 0.25) < 1e-9


def test_single_class_component_uses_the_defined_rate():
    qs = [_scored("a", _flags("outcome"), _flags("outcome", "covariates")),
          _scored("b", _flags(), _flags())]
    overall, _ = aggregate(qs)

    covariates = overall.components["covariates"]   # gold never unclear
    assert covariates.sensitivity is None
    assert covariates.specificity == 0.5
    assert covariates.single_class is True
    assert covariates.balanced_accuracy == 0.5
    assert overall.components["outcome"].balanced_accuracy == 1.0


def test_subtask_weights_are_configurable_and_renormalised():
    q = _scored("a", _flags("outcome"), _flags("outcome"))
    # No gold-unambiguous questions: analyze drops out and assess carries the
    # headline rather than analyze scoring zero.
    overall, _ = aggregate([q])
    assert overall.subtask_scores["analyze"] is None
    assert overall.overall_score == overall.subtask_scores["assess"]

    clear = _scored("b", _flags(), _flags())
    overall, _ = aggregate([q, clear], subtask_weights={"assess": 0.8, "analyze": 0.2})
    assert abs(overall.overall_score - (0.8 * overall.subtask_scores["assess"])) < 1e-9


def test_retired_subtask_weights_are_rejected():
    q = _scored("a", _flags("outcome"), _flags("outcome"))
    with pytest.raises(ValueError, match="unknown subtask weight"):
        aggregate([q], subtask_weights={"classify": 0.5, "disambiguate": 0.5})
