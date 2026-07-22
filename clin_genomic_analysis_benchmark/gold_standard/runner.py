"""Gold-standard pipeline driver.

For each unambiguous question in the cohort YAML:
  1. Codegen a script (Claude/Vertex).
  2. Dual-model review (Claude + Azure OpenAI).
     - Both approve → execute.
     - Both reject  → repair attempt with reviewer feedback (counts as one repair iter).
     - Disagreement → log to review queue, then execute optimistically (the
                      script is the test artifact; humans can curate later).
  3. Execute in sandbox.
  4. Validate result.json against the answer-type schema.
  5. On exec/validate failure, repair with the trace + previous body.

Updates `questions/<cohort>.yaml` in-place with `gold_answer` and
`gold_supporting_evidence` for each successful question.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Template

from .. import data_dictionary, sampling
from ..cohorts import Cohort, categorize_files
from ..config import PROMPTS_DIR, settings
from ..llm.azure_openai_client import AzureClient
from ..llm.vertex_client import CachedBlock, VertexClient
from ..questions import io as q_io
from ..questions.schema import (
    CohortQuestionFile,
    GoldAnswer,
    Question,
    SupportingEvidence,
)
from ..utils.jsonio import atomic_write_text
from . import reviewer as reviewer_mod
from . import sandbox as sandbox_mod
from . import script_writer
from .validator import load_and_validate

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_python_block(text: str) -> Optional[str]:
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).rstrip()
    # If the LLM returned bare code, accept it as long as it contains `def analyze`
    if "def analyze" in text:
        return text.rstrip()
    return None


def _build_cached_blocks(cohort: Cohort) -> list[CachedBlock]:
    variables = data_dictionary.load(cohort)
    ctx = sampling.build(cohort)
    return [
        CachedBlock(text=f"# COHORT DATA DICTIONARY ({cohort.name})\n\n"
                         + data_dictionary.to_compact_markdown(variables),
                    label="dictionary"),
        CachedBlock(text=f"# COHORT FILE INVENTORY & SAMPLES ({cohort.name})\n\n"
                         + sampling.to_compact_markdown(ctx),
                    label="cohort_context"),
    ]


def _render_codegen_user(cohort: Cohort, question: Question) -> str:
    tmpl = Template((PROMPTS_DIR / "gold_codegen_user.md.j2").read_text())
    files_by_category = {k: [p.name for p in v] for k, v in categorize_files(cohort).items() if v}
    return tmpl.render(cohort=cohort, question=question, files_by_category=files_by_category)


def _render_repair_user(question: Question, *, previous_body: str, failure_mode: str,
                        failure_output: str, validation_errors: list[str],
                        reviewer_feedback: str) -> str:
    tmpl = Template((PROMPTS_DIR / "gold_repair_user.md.j2").read_text())
    return tmpl.render(
        question=question,
        previous_body=previous_body,
        failure_mode=failure_mode,
        failure_output=failure_output,
        validation_errors=validation_errors,
        reviewer_feedback=reviewer_feedback,
    )


@dataclass
class GoldOutcome:
    question_id: str
    success: bool
    repair_attempts: int
    answer: Optional[dict]
    failure_reason: Optional[str]
    duration_seconds: float


def _codegen(*, codegen_client: VertexClient, cohort: Cohort, question: Question,
             user_prompt: str) -> Optional[str]:
    """Run the gold-standard codegen call with caching.

    `codegen_client` must expose `.generate(system_text=, cached_blocks=,
    user_text=, max_tokens=) -> ClaudeResponse`. The default `VertexClient`
    matches; this hook exists mostly for future swap-in of an alternative
    Claude-compatible client (e.g., direct Anthropic SDK, or a fine-tuned model).
    """
    system_prompt = (PROMPTS_DIR / "gold_codegen_system.md").read_text()
    cached = _build_cached_blocks(cohort)
    resp = codegen_client.generate(
        system_text=system_prompt,
        cached_blocks=cached,
        user_text=user_prompt,
        max_tokens=8000,
    )
    logger.info("  codegen tokens: in=%d out=%d cache_read=%d cache_create=%d",
                resp.input_tokens, resp.output_tokens, resp.cache_read_tokens,
                resp.cache_creation_tokens)
    return _extract_python_block(resp.text)


def compute_one(
    *,
    cohort: Cohort,
    question: Question,
    claude: VertexClient,
    azure: AzureClient,
    max_repair_iters: int = 3,
    sandbox_timeout_s: int = 300,
    codegen_client: Optional[VertexClient] = None,
) -> GoldOutcome:
    """Run the full pipeline for one unambiguous question."""
    start = time.monotonic()
    if question.classification != "unambiguous" or question.analysis_spec is None:
        return GoldOutcome(question.id, False, 0, None, "not unambiguous", 0.0)

    log_lines: list[str] = []

    def _log(msg: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"[{ts}] {msg}"
        log_lines.append(line)
        logger.info("%s", msg)

    answer_type = question.analysis_spec.expected_answer_type
    script_p = script_writer.script_path(cohort.name, question.id)
    result_p = script_writer.result_path(cohort.name, question.id)
    log_p = script_writer.log_path(cohort.name, question.id)

    # Initial prompt
    user_prompt = _render_codegen_user(cohort, question)
    _log(f"=== Q {question.id} (answer_type={answer_type}) ===")

    body: Optional[str] = None
    failure_reason: Optional[str] = None
    captured: Optional[dict] = None

    cg_client = codegen_client or claude

    for attempt in range(max_repair_iters + 1):
        _log(f"--- attempt {attempt + 1} ---")
        body = _codegen(codegen_client=cg_client, cohort=cohort,
                        question=question, user_prompt=user_prompt)
        if body is None:
            failure_reason = "codegen produced no python block"
            _log("ERROR: codegen produced no python block; cannot proceed")
            break

        # Persist the candidate script to disk so reviewers (and humans) can read it
        script_path = script_writer.write(
            cohort=cohort.name, qid=question.id, qtext=question.text,
            answer_type=answer_type, body=body,
        )

        # Dual-model review
        rev = reviewer_mod.review(
            cohort=cohort, question=question, script=script_path.read_text(),
            claude_client=claude, azure_client=azure,
        )
        for r in rev.reviewers:
            _log(f"  reviewer[{r.name}] approve={r.approve} issues={r.issues}")

        if rev.outcome == reviewer_mod.ReviewOutcome.REJECTED:
            # Both reviewers reject — repair using union of issues
            user_prompt = _render_repair_user(
                question, previous_body=body,
                failure_mode="reviewers rejected the candidate script",
                failure_output="(no execution attempted; reviewers rejected)",
                validation_errors=rev.union_issues,
                reviewer_feedback=rev.combined_suggested_fix,
            )
            failure_reason = "reviewers rejected"
            continue

        if rev.outcome == reviewer_mod.ReviewOutcome.DISAGREEMENT:
            queue_path = reviewer_mod.append_disagreement(
                cohort=cohort, question=question, script=script_path.read_text(),
                result=rev, attempt=attempt + 1,
            )
            _log(f"  REVIEWERS DISAGREE — flagged in {queue_path.name}; running script optimistically")

        # Execute (approved or disagreement; in both cases we run and validate)
        sb_result = sandbox_mod.run(
            script_path=script_path,
            cohort_dir=cohort.path,
            result_path=result_p,
            timeout_s=sandbox_timeout_s,
        )
        _log(f"  exec exit={sb_result.exit_code} timeout={sb_result.timed_out} dur={sb_result.duration_seconds:.1f}s")
        if sb_result.stderr:
            _log("  STDERR: " + sb_result.stderr.strip()[:400])
        if sb_result.stdout:
            _log("  STDOUT: " + sb_result.stdout.strip()[:200])

        if not sb_result.success:
            user_prompt = _render_repair_user(
                question, previous_body=body,
                failure_mode=("timed out" if sb_result.timed_out
                              else f"exited with code {sb_result.exit_code}"),
                failure_output=(sb_result.stderr or sb_result.stdout)[-4000:],
                validation_errors=[],
                reviewer_feedback=rev.combined_suggested_fix,
            )
            failure_reason = f"exec failure (exit={sb_result.exit_code})"
            continue

        # Validate the result
        captured, errs = load_and_validate(result_p, answer_type)
        if errs:
            _log(f"  VALIDATION ERRORS: {errs}")
            user_prompt = _render_repair_user(
                question, previous_body=body,
                failure_mode="result.json failed schema validation",
                failure_output=(result_p.read_text() if result_p.exists() else "")[:2000],
                validation_errors=errs,
                reviewer_feedback=rev.combined_suggested_fix,
            )
            failure_reason = f"validation failure: {errs[0] if errs else 'unknown'}"
            continue

        # Success!
        failure_reason = None
        _log(f"  SUCCESS: {captured}")
        break
    else:
        attempt += 1  # for accurate count when loop didn't break

    # Persist log
    log_p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(log_p, "\n".join(log_lines))

    duration = time.monotonic() - start
    return GoldOutcome(
        question_id=question.id,
        success=(captured is not None and failure_reason is None),
        repair_attempts=attempt,
        answer=captured,
        failure_reason=failure_reason,
        duration_seconds=duration,
    )


def compute_for_cohort(
    *,
    cohort: Cohort,
    only_qid: Optional[str] = None,
    only_qids: Optional[set[str]] = None,
    max_repair_iters: int = 3,
    sandbox_timeout_s: int = 300,
) -> tuple[CohortQuestionFile, list[GoldOutcome]]:
    cqf = q_io.load_gold(cohort.name)
    if cqf is None:
        raise FileNotFoundError(
            f"No gold questions YAML found for {cohort.name}; run generate-questions first."
        )

    targets = [q for q in cqf.questions if q.classification == "unambiguous"]
    forced = bool(only_qid) or bool(only_qids)
    if only_qid:
        targets = [q for q in targets if q.id == only_qid]
        if not targets:
            raise KeyError(f"Question {only_qid} not found or not unambiguous in {cohort.name}")
    elif only_qids is not None:
        targets = [q for q in targets if q.id in only_qids]

    claude = VertexClient.from_env()
    azure = AzureClient.from_env()

    outcomes: list[GoldOutcome] = []
    for q in targets:
        # Skip questions that already have a gold_answer unless --only/--only-file forces a redo
        if not forced and q.gold_answer is not None:
            logger.info("Skipping %s (already has gold_answer)", q.id)
            continue
        outcome = compute_one(
            cohort=cohort, question=q, claude=claude, azure=azure,
            max_repair_iters=max_repair_iters, sandbox_timeout_s=sandbox_timeout_s,
        )
        outcomes.append(outcome)
        if outcome.success and outcome.answer is not None:
            q.gold_answer = GoldAnswer(**outcome.answer)
            q.gold_supporting_evidence = SupportingEvidence(
                gold_script=str(script_writer.script_path(cohort.name, q.id)
                                .relative_to(settings().data_root.parent))
                if script_writer.script_path(cohort.name, q.id).is_relative_to(settings().data_root.parent)
                else str(script_writer.script_path(cohort.name, q.id)),
            )
        else:
            logger.warning("Failed gold-standard computation for %s: %s",
                           q.id, outcome.failure_reason)

    cqf.generated_at = datetime.now(timezone.utc)
    q_io.save_gold(cqf)
    return cqf, outcomes
