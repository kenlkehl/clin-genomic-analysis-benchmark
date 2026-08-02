"""Score-a-run driver."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

from ..config import RUNS_DIR
from ..llm.azure_openai_client import AzureClient
from ..llm.vertex_client import VertexClient
from ..questions import io as q_io
from ..utils.jsonio import atomic_write_text
from . import classification as cls_mod
from . import discrepancy as disc_mod
from . import judge as judge_mod
from . import report as report_mod
from .aggregator import QuestionScore, aggregate
from .types import JUDGE_MAX_PER_CONCEPT, DisambiguationScoreResult

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
    """Score a run dir and write scorecard.{json,md}.

    Disambiguation is scored by two LLM judges, each answering per gold concept
    "does the agent address this core concept at all?" as yes / unable to
    determine / no (2 / 1 / 0 pts). Their points are summed, so a concept is
    worth 0-4. There is no tie-break and no human step.
    """
    if Path(run_path).is_absolute():
        run_dir = Path(run_path)
    else:
        run_dir = (RUNS_DIR / run_path).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Run dir does not exist: {run_dir}")

    manifest, runs_raw = _load_run(run_dir)
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

    # Need clients only if any ambiguous question was attempted (judge required)
    claude_client: Optional[VertexClient] = None
    azure_client: Optional[AzureClient] = None

    question_scores: list[QuestionScore] = []

    for qrun in runs_raw:
        cohort = qrun["cohort"]
        qid = qrun["question_id"]
        gold_q = questions_by_cohort_id.get(cohort, {}).get(qid)
        if gold_q is None:
            logger.warning("Could not find gold question for %s/%s; skipping", cohort, qid)
            continue
        qs = QuestionScore(
            question_id=qid, cohort=cohort, category=qrun["category"],
            # Source gold classification from the gold bank, not from runs.json
            # (runs/ lives in the agent-reachable repo and is kept gold-free).
            gold_classification=gold_q.classification,
            gold_disambiguation_n=len(gold_q.disambiguation_concepts or []),
            disambig_points_per_concept=JUDGE_MAX_PER_CONCEPT,
        )

        # 1) Classification
        classify = qrun.get("classify") or {}
        if classify.get("success"):
            agent_label = (classify.get("result") or {}).get("classification")
            qs.classification = cls_mod.score(agent_label=agent_label,
                                              gold_label=gold_q.classification)
        else:
            qs.classification = cls_mod.score(agent_label=None, gold_label=gold_q.classification)
            qs.failure_reason = classify.get("failure_reason") or "classify failed"

        # 2) Disambiguation OR 3) Analysis
        agent_label = qs.classification.agent_label if qs.classification else None
        if agent_label == "ambiguous":
            disambig = qrun.get("disambiguate") or {}
            agent_concepts = list(((disambig.get("result") or {}).get("concepts")) or []) \
                if disambig.get("success") else []
            gold_concepts = list(gold_q.disambiguation_concepts or [])
            if not gold_concepts:
                # Gold says unambiguous; the agent went down disambig path → 0 pts
                qs.disambiguation = DisambiguationScoreResult(
                    question_id=qid, cohort=cohort, n_gold=0, points=0.0,
                    decisions=[], points_per_concept=JUDGE_MAX_PER_CONCEPT,
                )
            else:
                if claude_client is None:
                    claude_client = VertexClient.from_env()
                if azure_client is None:
                    azure_client = AzureClient.from_env()
                qs.disambiguation = judge_mod.score_disambiguation(
                    question_id=qid, cohort=cohort, question_text=gold_q.text,
                    gold_concepts=gold_concepts, agent_concepts=agent_concepts,
                    claude_client=claude_client, azure_client=azure_client,
                    raw_log_dir=run_dir / "per_question" / cohort / qid,
                )
        elif agent_label == "unambiguous":
            analyze = qrun.get("analyze") or {}
            gold_answer = (gold_q.gold_answer.model_dump(exclude_none=True)
                           if gold_q.gold_answer else None)
            if not analyze.get("success") or gold_answer is None:
                if gold_answer is None:
                    if gold_q.classification == "ambiguous":
                        qs.failure_reason = (
                            "gold question is ambiguous; agent classified it unambiguous, "
                            "so analysis was not scored"
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
                if not gold_atype:
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

    # A judge that returns no usable verdict is scored "unable to determine";
    # surface the count so a flaky endpoint cannot pass for a hard call.
    n_missing = sum(q.disambiguation.n_missing_verdicts for q in question_scores
                    if q.disambiguation is not None)
    if n_missing:
        logger.warning("%d judge verdicts were missing or unparseable and scored "
                       "'unable to determine'", n_missing)

    weights = scoring_config.get("subtask_weights") or None
    overall, per_cohort = aggregate(question_scores, subtask_weights=weights)

    md = report_mod.to_markdown(
        overall=overall, per_cohort=per_cohort,
        question_scores=question_scores,
        agent_name=agent_name, run_id=run_id,
        n_missing_verdicts=n_missing,
    )
    js = report_mod.to_json(
        overall=overall, per_cohort=per_cohort,
        question_scores=question_scores,
        agent_name=agent_name, run_id=run_id,
        n_missing_verdicts=n_missing,
    )
    atomic_write_text(run_dir / "scorecard.md", md)
    atomic_write_text(run_dir / "scorecard.json", js)
    return run_dir
