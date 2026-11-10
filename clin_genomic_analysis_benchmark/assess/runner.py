"""Run the assess task: one model call per question, with retries.

Writes runs/<run_name>/<run_id>/ with:

- ``manifest.json`` — task, model provenance, rulebook hashes, settings
- ``runs.json`` — one record per question, with the parsed verdicts
- ``rulebook.md`` — the exact rulebook text every prompt began with
- ``per_question/<cohort>/<qid>/`` — the question block, each raw reply, and
  ``assess.json`` when a reply validated

The model sees only the prompt, so no sandbox is involved. The question bank
read here is the public one; scoring reads the gold bank separately.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import shutil
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from ..cohorts import Cohort, get_cohort, resolve_cohorts
from ..config import RUNS_DIR
from ..instructions import assess_rulebook, rulebook_provenance
from ..questions import io as q_io
from ..questions.schema import PublicQuestion
from ..utils.jsonio import atomic_write_json, atomic_write_text
from .backends import Backend, BackendConfig, make_backend
from .prompt import AssessPrompt, build_assess_prompt, sha256_text
from .schema import validate_assess_result

logger = logging.getLogger(__name__)

TASK = "assess"
PRE_REPAIR_DIRNAME = "pre_repair"
INTEGRITY = {
    "status": "valid",
    "boundary": "prompt_only",
    "detail": "one API call per question; the model has no tools and no file access",
}


@dataclass
class AssessAttempt:
    attempt: int
    success: bool
    duration_seconds: float
    failure_reason: Optional[str]
    schema_errors: list[str]
    stop_reason: Optional[str] = None
    response_model: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AssessCall:
    success: bool
    result: Optional[dict]
    failure_reason: Optional[str]
    schema_errors: list[str]
    duration_seconds: float
    attempt_count: int
    attempts: list[AssessAttempt]


@dataclass
class AssessQuestionRun:
    question_id: str
    cohort: str
    category: int
    assess: Optional[dict] = None   # serialised AssessCall
    error: Optional[str] = None


# ---- reply parsing -------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _balanced_objects(text: str) -> list[str]:
    """Every top-level ``{...}`` span, respecting JSON string literals."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"' and depth > 0:
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start:i + 1])
    return spans


def parse_reply(text: str) -> tuple[Optional[dict], list[str], Optional[str]]:
    """Return (result, schema_errors, failure_reason) for a raw reply.

    Tries the whole reply, then fenced blocks, then every balanced object, and
    takes the last candidate that validates (a reply may quote its own draft
    before the final answer). If none validates, reports the last JSON object.
    """
    stripped = text.strip()
    if not stripped:
        return None, [], "empty reply"
    candidates = [stripped] + _FENCE_RE.findall(stripped) + _balanced_objects(stripped)
    parsed: list[dict] = []
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    if not parsed:
        return None, [], "no JSON object in reply"
    for obj in reversed(parsed):
        if not validate_assess_result(obj):
            return obj, [], None
    last = parsed[-1]
    return last, validate_assess_result(last), "schema_violation"


# ---- one question ----------------------------------------------------------------

def call_model(*, backend: Backend, prompt: AssessPrompt, qdir: Path, max_attempts: int,
               retry_base_seconds: float) -> AssessCall:
    """Call the model until a reply validates or attempts run out."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    qdir.mkdir(parents=True, exist_ok=True)
    overall_start = time.monotonic()
    attempts: list[AssessAttempt] = []
    result: Optional[dict] = None
    failure_reason: Optional[str] = None
    schema_errors: list[str] = []
    for number in range(1, max_attempts + 1):
        start = time.monotonic()
        stop_reason = response_model = None
        usage: dict[str, int] = {}
        try:
            completion = backend.complete(prompt)
        except Exception as exc:  # any SDK, network, or auth failure is retried
            result, schema_errors = None, []
            failure_reason = f"backend error: {type(exc).__name__}: {exc}"[:1000]
            atomic_write_text(qdir / f"response.{number}.txt", f"[{failure_reason}]\n")
        else:
            stop_reason = completion.stop_reason
            response_model = completion.response_model
            usage = completion.usage
            atomic_write_text(qdir / f"response.{number}.txt", completion.text)
            result, schema_errors, failure_reason = parse_reply(completion.text)
            if failure_reason and stop_reason in {"max_tokens", "length"}:
                failure_reason = f"{failure_reason} (reply truncated: stop_reason={stop_reason})"
        attempts.append(AssessAttempt(
            attempt=number, success=failure_reason is None,
            duration_seconds=time.monotonic() - start, failure_reason=failure_reason,
            schema_errors=schema_errors, stop_reason=stop_reason,
            response_model=response_model, usage=usage,
        ))
        if failure_reason is None:
            atomic_write_json(qdir / "assess.json", result)
            break
        if number < max_attempts:
            delay = retry_base_seconds * (2 ** (number - 1))
            logger.warning("assess %s attempt %d/%d failed (%s); retrying in %.1fs",
                           qdir.name, number, max_attempts, failure_reason, delay)
            if delay:
                time.sleep(delay)
    return AssessCall(
        success=failure_reason is None,
        result=result if failure_reason is None else None,
        failure_reason=failure_reason,
        schema_errors=schema_errors,
        duration_seconds=time.monotonic() - overall_start,
        attempt_count=len(attempts),
        attempts=attempts,
    )


def _assess_one(*, backend: Backend, rulebook: str, question: PublicQuestion, cohort: Cohort,
                qdir: Path, max_attempts: int, retry_base_seconds: float) -> AssessQuestionRun:
    qrun = AssessQuestionRun(question_id=question.id, cohort=cohort.name,
                             category=question.category)
    try:
        prompt = build_assess_prompt(question_text=question.text, cohort=cohort,
                                     rulebook=rulebook)
        qdir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(qdir / "question.md", prompt.question)
        call = call_model(backend=backend, prompt=prompt, qdir=qdir,
                          max_attempts=max_attempts, retry_base_seconds=retry_base_seconds)
        qrun.assess = asdict(call)
    except Exception as exc:
        logger.exception("assess %s blew up", question.id)
        qrun.error = repr(exc)
    return qrun


def _run_all(work: list[tuple[Cohort, PublicQuestion]], *, backend: Backend, rulebook: str,
             per_question_root: Path, max_parallel: int, max_attempts: int,
             retry_base_seconds: float) -> list[AssessQuestionRun]:
    def one(item: tuple[Cohort, PublicQuestion]) -> AssessQuestionRun:
        cohort, question = item
        return _assess_one(backend=backend, rulebook=rulebook, question=question,
                           cohort=cohort, qdir=per_question_root / cohort.name / question.id,
                           max_attempts=max_attempts, retry_base_seconds=retry_base_seconds)

    if max_parallel <= 1:
        return [one(item) for item in work]
    runs: list[AssessQuestionRun] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(one, item) for item in work]
        for future in as_completed(futures):
            runs.append(future.result())
    return runs


def _sorted(runs: list[dict]) -> list[dict]:
    return sorted(runs, key=lambda r: (r["cohort"], r["question_id"]))


def _n_completed(runs: list[dict]) -> int:
    return sum(1 for r in runs if (r.get("assess") or {}).get("success"))


def _rulebook_record(rulebook: str) -> dict[str, str]:
    return {**rulebook_provenance(TASK), "assembled_sha256": sha256_text(rulebook)}


def default_run_name(model_spec: str) -> str:
    return "assess_" + re.sub(r"[^A-Za-z0-9._-]+", "-", model_spec).strip("-")


def run_assess(
    *,
    backend_config: BackendConfig,
    run_name: str,
    cohort_spec: str = "all",
    question_ids: Sequence[str] = (),
    max_parallel: int = 4,
    max_attempts: int = 3,
    retry_base_seconds: float = 5.0,
    run_id: Optional[str] = None,
    backend: Optional[Backend] = None,
) -> Path:
    """Assess every matching public question (all of them when
    ``question_ids`` is empty). Returns the run directory.

    ``backend`` overrides the one built from ``backend_config`` (for tests).
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be >= 1")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds must be >= 0")
    cohorts = resolve_cohorts(cohort_spec)
    work: list[tuple[Cohort, PublicQuestion]] = []
    for cohort in cohorts:
        bank = q_io.load_public(cohort.name)
        if bank is None:
            logger.warning("No public questions YAML for %s; skipping.", cohort.name)
            continue
        work.extend((cohort, q) for q in bank.questions
                    if not question_ids or q.id in question_ids)
    if not work:
        raise ValueError("No questions matched the requested cohort/question filter.")
    unmatched = sorted(set(question_ids) - {q.id for _, q in work})
    if unmatched:
        raise ValueError(f"No such question(s) in the requested cohorts: {', '.join(unmatched)}")

    backend = backend or make_backend(backend_config)
    rulebook = assess_rulebook()
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    run_dir = RUNS_DIR / run_name / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    atomic_write_text(run_dir / "rulebook.md", rulebook)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info("Assess: %d question(s) across %d cohort(s); parallel=%d",
                len(work), len(cohorts), max_parallel)

    runs = _sorted([asdict(r) for r in _run_all(
        work, backend=backend, rulebook=rulebook, per_question_root=run_dir / "per_question",
        max_parallel=max_parallel, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds)])
    manifest = {
        "task": TASK,
        "run_name": run_name,
        "run_id": run_id,
        "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "cohorts": [c.name for c in cohorts],
        "n_questions": len(work),
        "n_completed": _n_completed(runs),
        "model_provenance": backend.provenance(),
        "backend_config": backend.config.to_dict(),
        "rulebook": _rulebook_record(rulebook),
        "integrity": dict(INTEGRITY),
        "settings": {
            "question_filter": sorted(question_ids) or None,
            "max_parallel": max_parallel,
            "retries": {"max_attempts": max_attempts,
                        "base_delay_seconds": retry_base_seconds,
                        "backoff": "exponential"},
        },
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    atomic_write_json(run_dir / "runs.json", runs)
    return run_dir


# ---- retrying failures -------------------------------------------------------------

@dataclass(frozen=True)
class AssessRetrySummary:
    source_run_dir: Path
    retried_run_dir: Path
    n_targets: int
    n_recovered: int


def _resolve(run_path: str | Path) -> Path:
    path = Path(run_path)
    resolved = path.resolve() if path.is_absolute() else (RUNS_DIR / path).resolve()
    for name in ("manifest.json", "runs.json"):
        if not (resolved / name).is_file():
            raise FileNotFoundError(f"Run is missing {name}: {resolved}")
    return resolved


def failed_questions(runs: list[dict]) -> list[dict]:
    """Questions whose assess call never produced a valid reply."""
    return [r for r in runs if not (r.get("assess") or {}).get("success")]


def retry_assess_failures(
    *,
    run_path: str | Path,
    max_parallel: int = 2,
    max_attempts: int = 5,
    retry_base_seconds: float = 5.0,
    output_run_id: Optional[str] = None,
    backend: Optional[Backend] = None,
) -> AssessRetrySummary:
    """Re-call the model for failed questions in a copy of the run.

    The copy keeps every successful reply, uses the model settings recorded in
    the source manifest, and refuses to run if a rulebook file has changed
    since the source run. The source is then moved under ``pre_repair/`` so
    only the retried copy sits directly under the run name.
    """
    source_dir = _resolve(run_path)
    manifest = json.loads((source_dir / "manifest.json").read_text())
    runs = json.loads((source_dir / "runs.json").read_text())
    if manifest.get("task") != TASK:
        raise ValueError(f"{source_dir} is not an assess run")
    targets = failed_questions(runs)
    if not targets:
        raise ValueError(f"No failed assess calls in {source_dir}")

    rulebook = assess_rulebook()
    recorded = manifest.get("rulebook") or {}
    if recorded.get("assembled_sha256") != sha256_text(rulebook):
        raise ValueError(
            "the assess rulebook or conventions changed since this run; rerun the task "
            "instead of mixing replies to two rulebooks")
    backend = backend or make_backend(BackendConfig.from_dict(manifest["backend_config"]))

    source_id = str(manifest.get("run_id") or source_dir.name)
    retried_id = output_run_id or f"{source_id}-retry-{uuid.uuid4().hex[:6]}"
    collection = source_dir.parent
    retried_dir = collection / retried_id
    if retried_dir.exists():
        raise FileExistsError(f"Retry destination already exists: {retried_dir}")
    shutil.copytree(source_dir, retried_dir, ignore=lambda _d, names: [
        n for n in names if n in {"scorecard.json", "scorecard.md"}])

    work = []
    public = {}
    for cohort_name in sorted({t["cohort"] for t in targets}):
        bank = q_io.load_public(cohort_name)
        if bank is None:
            raise FileNotFoundError(f"No public question bank for {cohort_name}")
        public.update({(cohort_name, q.id): q for q in bank.questions})
    for target in targets:
        key = (target["cohort"], target["question_id"])
        if key not in public:
            raise ValueError(f"Public question not found: {key[0]}/{key[1]}")
        work.append((get_cohort(key[0]), public[key]))

    retry_root = retried_dir / "retries" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    replacements = _run_all(work, backend=backend, rulebook=rulebook,
                            per_question_root=retry_root, max_parallel=max_parallel,
                            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds)
    by_key = {(r["cohort"], r["question_id"]): r for r in runs}
    recovered = 0
    outcomes: list[dict[str, Any]] = []
    for replacement in replacements:
        raw = asdict(replacement)
        call = raw.get("assess") or {}
        merged = bool(call.get("success"))
        if merged:
            by_key[(replacement.cohort, replacement.question_id)].update(
                assess=raw["assess"], error=None)
            qdir = retried_dir / "per_question" / replacement.cohort / replacement.question_id
            atomic_write_json(qdir / "assess.json", call["result"])
            recovered += 1
        outcomes.append({"cohort": replacement.cohort, "question_id": replacement.question_id,
                         "merged": merged, "failure_reason": call.get("failure_reason")})

    runs = _sorted(list(by_key.values()))
    atomic_write_json(retried_dir / "runs.json", runs)
    archived = collection / PRE_REPAIR_DIRNAME / source_dir.name
    manifest.update({
        "run_id": retried_id,
        "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_completed": _n_completed(runs),
        "derived_from_run": str(archived.relative_to(RUNS_DIR)) if archived.is_relative_to(
            RUNS_DIR) else str(archived),
    })
    manifest.setdefault("retry_history", []).append({
        "source_run_id": source_id,
        "artifacts": str(retry_root.relative_to(retried_dir)),
        "retry_settings": {"max_parallel": max_parallel, "max_attempts": max_attempts,
                           "base_delay_seconds": retry_base_seconds},
        "model_provenance": backend.provenance(),
        "targets": outcomes,
        "recovered": recovered,
    })
    atomic_write_json(retried_dir / "manifest.json", manifest)

    archived.parent.mkdir(parents=True, exist_ok=True)
    if archived.exists():
        raise FileExistsError(f"Archive destination already exists: {archived}")
    shutil.move(str(source_dir), str(archived))
    return AssessRetrySummary(source_run_dir=archived, retried_run_dir=retried_dir,
                              n_targets=len(targets), n_recovered=recovered)
