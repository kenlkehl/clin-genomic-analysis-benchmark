"""Score-a-run driver. An assess run and an analyze run are scored separately;
``combine`` weights the two into the headline."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from ..components import COMPONENT_IDS, flags_from_assess_result
from ..config import RUNS_DIR
from ..questions import io as q_io
from ..utils.jsonio import atomic_write_text
from . import discrepancy as disc_mod
from . import report as report_mod
from .aggregator import QuestionScore, aggregate_analyze, aggregate_assess
from .assessment import score_assessment

logger = logging.getLogger(__name__)

TASKS = ("assess", "analyze")


def resolve_run_dir(run_path: str | Path) -> Path:
    path = Path(run_path)
    run_dir = path if path.is_absolute() else (RUNS_DIR / path).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run dir does not exist: {run_dir}")
    return run_dir


def _load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    runs = json.loads((run_dir / "runs.json").read_text())
    return manifest, runs


def load_scoring_config(path: Optional[Path]) -> dict:
    config: dict = {}
    if path and Path(path).exists():
        config = yaml.safe_load(Path(path).read_text()) or {}
    if "disambiguation" in config:
        raise ValueError(
            "scoring config has a 'disambiguation' block; concept-ID scoring was retired "
            "with the four-component assess task")
    return config


def _scoring_options(answer_type: str, question, scoring_config: dict) -> dict:
    """Per-question overrides from scoring_configs/default.yaml."""
    defaults = (scoring_config.get("defaults") or {}).get(answer_type, {})
    overrides = ((scoring_config.get("overrides") or {}).get(question.id) or {}).get(answer_type, {})
    return {**defaults, **overrides}


def _gold_questions(cohorts: list[str]) -> dict[str, dict[str, Any]]:
    """Scoring reads the GOLD bank (out-of-repo)."""
    out: dict[str, dict[str, Any]] = {}
    for cohort_name in cohorts:
        cqf = q_io.load_gold(cohort_name)
        if cqf is not None:
            out[cohort_name] = {q.id: q for q in cqf.questions}
    return out


def _score_assess_question(qrun: dict, gold_q) -> QuestionScore:
    qid, cohort = qrun["question_id"], qrun["cohort"]
    if gold_q.component_assessment is None:
        raise ValueError(
            f"gold question {qid} has no component_assessment; review it under the "
            "four-component rubric before scoring")
    qs = QuestionScore(question_id=qid, cohort=cohort, category=qrun["category"],
                       gold_classification=gold_q.classification)
    call = qrun.get("assess") or {}
    agent_flags = None
    rationales: dict[str, str] = {}
    failure = None
    if call.get("success"):
        result = call.get("result") or {}
        try:
            agent_flags = flags_from_assess_result(result)
        except (KeyError, TypeError, ValueError) as exc:
            failure = f"assess reply unreadable: {exc}"
        else:
            rationales = {cid: str((result.get(cid) or {}).get("rationale") or "")
                          for cid in COMPONENT_IDS}
    else:
        failure = call.get("failure_reason") or qrun.get("error") or "assess call failed"
    qs.assessment = score_assessment(
        question_id=qid, cohort=cohort,
        gold_flags=gold_q.component_assessment.flags(),
        agent_flags=agent_flags, agent_rationales=rationales, failure_reason=failure,
    )
    qs.failure_reason = failure
    return qs


def _score_analyze_question(qrun: dict, gold_q, scoring_config: dict) -> QuestionScore:
    qid, cohort = qrun["question_id"], qrun["cohort"]
    qs = QuestionScore(question_id=qid, cohort=cohort, category=qrun["category"],
                       gold_classification=gold_q.classification)
    analyze = qrun.get("analyze") or {}
    gold_answer = (gold_q.gold_answer.model_dump(exclude_none=True)
                   if gold_q.gold_answer else None)
    gold_atype = gold_q.analysis_spec.expected_answer_type if gold_q.analysis_spec else None
    qs.answer_type = gold_atype
    if gold_answer is None:
        qs.failure_reason = "gold_answer missing — run compute-gold first"
        return qs
    if not analyze.get("success"):
        qs.failure_reason = analyze.get("failure_reason") or qrun.get("error") or "analyze failed"
        return qs
    agent_result = analyze.get("result") or {}
    agent_atype = agent_result.get("answer_type")
    if gold_answer.get("unanswerable") is True:
        # Gold unanswerable → only the agent's unanswerable flag is scored.
        # Agents are not told the expected answer_type, and with no estimate
        # to compare the type carries no signal.
        qs.analysis = disc_mod.score_analysis(
            agent_answer=(agent_result.get("answer") or {}),
            gold_answer=gold_answer, answer_type=gold_atype)
    elif not gold_atype:
        qs.failure_reason = "gold has no expected_answer_type"
    elif agent_atype != gold_atype:
        qs.analysis = disc_mod.DiscrepancyResult(
            band=disc_mod.Band.MAJOR, points=0,
            metric="answer_type_mismatch", metric_value=None,
            explanation=f"agent answer_type={agent_atype!r} != gold {gold_atype!r}",
        )
    else:
        qs.analysis = disc_mod.score_analysis(
            agent_answer=(agent_result.get("answer") or {}),
            gold_answer=gold_answer, answer_type=gold_atype,
            options=_scoring_options(gold_atype, gold_q, scoring_config),
        )
    return qs


def _expected(task: str, gold_q) -> bool:
    """Whether a gold question belongs in a full run of ``task``."""
    if task == "assess":
        return True
    return gold_q.classification == "unambiguous"


def score_run(
    *,
    run_path: str,
    scoring_config_path: Optional[Path] = None,
) -> Path:
    """Score a run deterministically and write scorecard.{json,md}."""
    run_dir = resolve_run_dir(run_path)
    manifest, runs_raw = _load_run(run_dir)
    task = manifest.get("task")
    if task not in TASKS:
        raise ValueError(
            f"{run_dir} has no assess/analyze task in its manifest; runs from before "
            "the assess/analyze split must be scored with an earlier version of the harness")
    integrity_status = str((manifest.get("integrity") or {}).get("status") or "unaudited")
    if integrity_status == "quarantined":
        raise ValueError(
            "refusing to score a quarantined run; resolve the integrity finding or use "
            "the narrowly scoped adjudication command when its policy applies"
        )
    scoring_config = load_scoring_config(scoring_config_path)
    cohorts = list(manifest.get("cohorts", []))
    gold = _gold_questions(cohorts)

    question_scores: list[QuestionScore] = []
    excluded: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for qrun in runs_raw:
        cohort, qid = qrun["cohort"], qrun["question_id"]
        seen.add((cohort, qid))
        gold_q = gold.get(cohort, {}).get(qid)
        if gold_q is None:
            logger.warning("Could not find gold question for %s/%s; skipping", cohort, qid)
            excluded.append({"cohort": cohort, "question_id": qid, "reason": "not in gold bank"})
            continue
        if task == "assess":
            question_scores.append(_score_assess_question(qrun, gold_q))
        elif not _expected(task, gold_q):
            # Gold was relabelled after the run chose its questions.
            excluded.append({"cohort": cohort, "question_id": qid,
                             "reason": "gold is no longer unambiguous"})
        else:
            question_scores.append(_score_analyze_question(qrun, gold_q, scoring_config))

    # A full run should cover every question its task expects; list any it
    # missed (for example, gold relabelled after the run) rather than score them.
    missing: list[dict[str, str]] = []
    if not (manifest.get("settings") or {}).get("question_filter"):
        for cohort in cohorts:
            for qid, gold_q in sorted(gold.get(cohort, {}).items()):
                if (cohort, qid) not in seen and _expected(task, gold_q):
                    missing.append({"cohort": cohort, "question_id": qid})

    if task == "assess":
        overall, per_cohort = aggregate_assess(question_scores)
        provenance = manifest.get("model_provenance") or {}
        run_name = manifest.get("run_name") or run_dir.parent.name
    else:
        overall, per_cohort = aggregate_analyze(question_scores)
        provenance = manifest.get("agent_provenance") or {}
        run_name = manifest.get("agent_name") or run_dir.parent.name

    card = report_mod.Scorecard(
        task=task, run_name=run_name, run_id=manifest.get("run_id") or run_dir.name,
        provenance=provenance, integrity=manifest.get("integrity") or {"status": "unaudited"},
        rulebook=manifest.get("rulebook") or {}, cohorts=cohorts,
        gold_bank_sha256=q_io.gold_bank_sha256(cohorts),
        overall=overall, per_cohort=per_cohort, question_scores=question_scores,
        excluded=excluded, missing=missing,
    )
    atomic_write_text(run_dir / "scorecard.md", report_mod.to_markdown(card))
    atomic_write_text(run_dir / "scorecard.json", report_mod.to_json(card))
    return run_dir
