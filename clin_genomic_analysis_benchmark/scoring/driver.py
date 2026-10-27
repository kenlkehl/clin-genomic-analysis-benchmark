"""Score-a-run driver."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

from ..components import COMPONENT_IDS, flags_from_assess_result
from ..config import RUNS_DIR
from ..questions import io as q_io
from ..utils.jsonio import atomic_write_text
from . import discrepancy as disc_mod
from . import report as report_mod
from .aggregator import QuestionScore, aggregate
from .assessment import score_assessment

logger = logging.getLogger(__name__)


def _load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    runs = json.loads((run_dir / "runs.json").read_text())
    return manifest, runs


def _scoring_options(answer_type: str, question, scoring_config: dict) -> dict:
    """Per-question overrides from scoring_configs/default.yaml."""
    defaults = (scoring_config.get("defaults") or {}).get(answer_type, {})
    overrides = ((scoring_config.get("overrides") or {}).get(question.id) or {}).get(answer_type, {})
    return {**defaults, **overrides}


def score_run(
    *,
    run_path: str,
    scoring_config_path: Optional[Path] = None,
) -> Path:
    """Score a run deterministically and write scorecard.{json,md}."""
    if Path(run_path).is_absolute():
        run_dir = Path(run_path)
    else:
        run_dir = (RUNS_DIR / run_path).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run dir does not exist: {run_dir}")

    manifest, runs_raw = _load_run(run_dir)
    integrity_status = str((manifest.get("integrity") or {}).get("status") or "unaudited")
    if integrity_status == "quarantined":
        raise ValueError(
            "refusing to score a quarantined run; resolve the integrity finding or use "
            "the narrowly scoped adjudication command when its policy applies"
        )
    agent_name = manifest["agent_name"]
    run_id = manifest["run_id"]

    scoring_config: dict = {}
    if scoring_config_path and Path(scoring_config_path).exists():
        scoring_config = yaml.safe_load(Path(scoring_config_path).read_text()) or {}

    # Cache cohort question files — scoring reads the GOLD bank (out-of-repo).
    questions_by_cohort_id: dict[str, dict[str, object]] = {}
    for cohort_name in manifest.get("cohorts", []):
        cqf = q_io.load_gold(cohort_name)
        if cqf is not None:
            questions_by_cohort_id[cohort_name] = {q.id: q for q in cqf.questions}

    if "disambiguation" in scoring_config:
        raise ValueError(
            "scoring config has a 'disambiguation' block; concept-ID scoring was retired "
            "with the four-component assess stage")

    question_scores: list[QuestionScore] = []

    for qrun in runs_raw:
        cohort = qrun["cohort"]
        qid = qrun["question_id"]
        gold_q = questions_by_cohort_id.get(cohort, {}).get(qid)
        if gold_q is None:
            logger.warning("Could not find gold question for %s/%s; skipping", cohort, qid)
            continue
        if gold_q.component_assessment is None:
            raise ValueError(
                f"gold question {qid} has no component_assessment; review it under the "
                "four-component rubric before scoring")
        qs = QuestionScore(
            question_id=qid, cohort=cohort, category=qrun["category"],
            # Source gold classification from the gold bank, not from runs.json
            # (runs/ lives in the agent-reachable repo and is kept gold-free).
            gold_classification=gold_q.classification,
        )

        # 1) Assessment: one verdict per component.
        assess = qrun.get("assess") or {}
        agent_flags = None
        rationales: dict[str, str] = {}
        assess_failure = None
        if assess.get("success"):
            result = assess.get("result") or {}
            try:
                agent_flags = flags_from_assess_result(result)
            except (KeyError, TypeError, ValueError) as exc:
                assess_failure = f"assess result unreadable: {exc}"
            else:
                rationales = {
                    cid: str((result.get(cid) or {}).get("rationale") or "")
                    for cid in COMPONENT_IDS
                }
        else:
            assess_failure = assess.get("failure_reason") or "assess failed"
        qs.assessment = score_assessment(
            question_id=qid, cohort=cohort,
            gold_flags=gold_q.component_assessment.flags(),
            agent_flags=agent_flags,
            agent_rationales=rationales,
            failure_reason=assess_failure,
        )
        if assess_failure:
            qs.failure_reason = assess_failure

        # 2) Analysis, only when the agent found every component clear.
        agent_label = qs.assessment.agent_classification
        if agent_label == "ambiguous" and gold_q.classification == "unambiguous":
            qs.failure_reason = (
                "agent flagged a component of a gold-unambiguous question, so analysis "
                "was not attempted")
        elif agent_label == "unambiguous":
            analyze = qrun.get("analyze") or {}
            gold_answer = (gold_q.gold_answer.model_dump(exclude_none=True)
                           if gold_q.gold_answer else None)
            if not analyze.get("success") or gold_answer is None:
                if gold_answer is None:
                    if gold_q.classification == "ambiguous":
                        qs.failure_reason = (
                            "gold question is ambiguous; agent found every component "
                            "clear, so analysis was not scored"
                        )
                    else:
                        qs.failure_reason = "gold_answer missing — run compute-gold first"
                else:
                    qs.failure_reason = analyze.get("failure_reason") or "analyze failed"
                # 0 points for analysis
            else:
                agent_result = analyze.get("result") or {}
                agent_atype = agent_result.get("answer_type")
                gold_atype = gold_q.analysis_spec.expected_answer_type if gold_q.analysis_spec else None
                if gold_answer.get("unanswerable") is True:
                    # Gold unanswerable → only the agent's unanswerable flag is
                    # scored. Agents are not told the expected answer_type, and
                    # with no estimate to compare the type carries no signal.
                    qs.answer_type = gold_atype
                    qs.analysis = disc_mod.score_analysis(
                        agent_answer=(agent_result.get("answer") or {}),
                        gold_answer=gold_answer,
                        answer_type=gold_atype,
                    )
                elif not gold_atype:
                    qs.failure_reason = "gold has no expected_answer_type"
                elif agent_atype != gold_atype:
                    # Wrong answer-type → 0 pts (MAJOR)
                    qs.answer_type = gold_atype
                    qs.analysis = disc_mod.DiscrepancyResult(
                        band=disc_mod.Band.MAJOR, points=0,
                        metric="answer_type_mismatch", metric_value=None,
                        explanation=f"agent answer_type={agent_atype!r} != gold {gold_atype!r}",
                    )
                else:
                    qs.answer_type = gold_atype
                    options = _scoring_options(gold_atype, gold_q, scoring_config)
                    qs.analysis = disc_mod.score_analysis(
                        agent_answer=(agent_result.get("answer") or {}),
                        gold_answer=gold_answer,
                        answer_type=gold_atype,
                        options=options,
                    )

        question_scores.append(qs)

    weights = scoring_config.get("subtask_weights") or None
    overall, per_cohort = aggregate(question_scores, subtask_weights=weights)

    md = report_mod.to_markdown(
        overall=overall, per_cohort=per_cohort,
        question_scores=question_scores,
        agent_name=agent_name, run_id=run_id,
        agent_provenance=manifest.get("agent_provenance"),
        integrity=manifest.get("integrity"),
    )
    js = report_mod.to_json(
        overall=overall, per_cohort=per_cohort,
        question_scores=question_scores,
        agent_name=agent_name, run_id=run_id,
        agent_provenance=manifest.get("agent_provenance"),
        integrity=manifest.get("integrity"),
    )
    atomic_write_text(run_dir / "scorecard.md", md)
    atomic_write_text(run_dir / "scorecard.json", js)
    return run_dir
