"""3-stage flow per question:
  1. classify
  2. if classified ambiguous   → disambiguate
     if classified unambiguous → analyze

Writes per-question artifacts under runs/<agent_name>/<run_id>/per_question/<cohort>/<qid>/.
Builds a per-run manifest.json with agent_cmd, run start/end, env (no secrets).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import socket
import tomllib
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..cohorts import Cohort, resolve_cohorts
from ..concepts import concept_menu_payload
from ..config import RUNS_DIR, TimeoutConfig, settings
from ..questions import io as q_io
from ..questions.schema import PublicQuestion
from ..utils.jsonio import atomic_write_json
from .runner import StageInvocation, invoke

logger = logging.getLogger(__name__)


_DEFAULT_INSTRUCTIONS = {
    "classify": (
        "You are evaluating one analysis question against AACR Project GENIE BPC cohort data. "
        "The cohort folder and a data dictionary are provided. Your task is to decide whether "
        "the question is ANSWERABLE deterministically given the data, or whether it is "
        "AMBIGUOUS such that two competent analysts could reasonably compute different numbers. "
        "A question is ambiguous when one or more items from the supplied concept menu must be "
        "specified before a deterministic analysis can be chosen. "
        "Read enough of the data dictionary and file inventory to make an informed call. "
        "Return strictly: {\"classification\": \"ambiguous\"|\"unambiguous\", \"rationale\": \"...\"}."
    ),
    "disambiguate": (
        "You previously classified this question as AMBIGUOUS. Select every material concept the "
        "questioner must specify to make the analysis deterministic, using only IDs from "
        "disambiguation_concept_menu. Select an ID only when that choice is genuinely unresolved "
        "after applying the benchmark conventions; incorrect selections are penalized. "
        "Return strictly: {\"concept_ids\": [\"MENU_ID\", \"...\"]}."
    ),
    "analyze": (
        "You previously classified this question as UNAMBIGUOUS. Compute the answer using the "
        "cohort data files. Use Python (pandas/numpy/scipy/statsmodels/lifelines as needed). "
        "Return strictly: "
        "{\"answer_type\": \"count|proportion|median_with_ci|hazard_ratio_with_ci|odds_ratio_with_ci|pvalue|categorical|categorical_distribution\", "
        "\"answer\": {<typed fields>}, \"methods\": \"...\", \"supporting_evidence\": {...}}."
    ),
}


@dataclass
class QuestionRun:
    question_id: str
    cohort: str
    category: int
    classification_gold: str
    classify: Optional[dict] = None        # serialised StageInvocation
    disambiguate: Optional[dict] = None
    analyze: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class RunManifest:
    agent_cmd: str
    agent_name: str
    run_id: str
    started_at: str
    ended_at: Optional[str]
    host: str
    platform: str
    cohorts: list[str]
    n_questions: int
    n_completed: int
    agent_provenance: dict
    settings: dict


_SAFE_AGENT_ENV_VARS = (
    "CLINGEN_CLAUDE_MODEL",
    "CLINGEN_CLAUDE_EFFORT",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_REGION",
    "ANTHROPIC_MODEL",
    "CODEX_MODEL",
    "CODEX_MODEL_PROVIDER",
    "CODEX_REASONING_EFFORT",
    "CODEX_PROFILE",
    "CODEX_SANDBOX_MODE",
)


def _read_codex_config(path: Path) -> dict:
    """Read a Codex TOML layer, returning no data when it is unavailable."""
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return config if isinstance(config, dict) else {}


def _codex_config_value(
    *,
    env: Mapping[str, str],
    env_var: str,
    config_key: str,
    profile_name: str | None,
    profile_config: dict,
    base_config: dict,
    default: str | None = None,
) -> tuple[str | None, str]:
    explicit = env.get(env_var, "").strip()
    if explicit:
        return explicit, env_var
    profile_value = profile_config.get(config_key)
    if profile_value is not None and str(profile_value).strip():
        return str(profile_value).strip(), f"codex_profile:{profile_name}"
    base_value = base_config.get(config_key)
    if base_value is not None and str(base_value).strip():
        return str(base_value).strip(), "codex_user_config"
    return default, "codex_default" if default is not None else "unresolved"


def _codex_provenance(
    *, agent_cmd: str, env: Mapping[str, str], read_user_config: bool
) -> dict | None:
    """Resolve effective Codex model settings without capturing credentials."""
    adapter_match = re.search(r"adapters/(codex_[^/\s\"']+)/", agent_cmd)
    if adapter_match is None:
        return None

    explicit_profile_name = env.get("CODEX_PROFILE", "").strip() or None
    base_config: dict = {}
    profile_config: dict = {}
    if read_user_config:
        codex_home_value = env.get("CODEX_HOME", "").strip()
        codex_home = (Path(codex_home_value).expanduser() if codex_home_value
                      else Path.home() / ".codex")
        base_config = _read_codex_config(codex_home / "config.toml")
    configured_profile = str(base_config.get("profile", "")).strip() or None
    profile_name = explicit_profile_name or configured_profile
    if read_user_config:
        if (profile_name
                and Path(profile_name).name == profile_name
                and profile_name not in {".", ".."}):
            profile_config = _read_codex_config(
                codex_home / f"{profile_name}.config.toml"
            )

    model, model_source = _codex_config_value(
        env=env,
        env_var="CODEX_MODEL",
        config_key="model",
        profile_name=profile_name,
        profile_config=profile_config,
        base_config=base_config,
    )
    provider, provider_source = _codex_config_value(
        env=env,
        env_var="CODEX_MODEL_PROVIDER",
        config_key="model_provider",
        profile_name=profile_name,
        profile_config=profile_config,
        base_config=base_config,
        default="openai",
    )
    effort, effort_source = _codex_config_value(
        env=env,
        env_var="CODEX_REASONING_EFFORT",
        config_key="model_reasoning_effort",
        profile_name=profile_name,
        profile_config=profile_config,
        base_config=base_config,
    )

    provenance = {
        "adapter": adapter_match.group(1),
        "provider": provider,
        "provider_source": provider_source,
        "model": model,
        "model_source": model_source,
        "effort_level": effort,
        "effort_supported": True,
        "effort_source": effort_source,
    }
    if profile_name:
        provenance["profile"] = profile_name
        provenance["profile_source"] = (
            "CODEX_PROFILE" if explicit_profile_name else "codex_user_config"
        )
    return provenance


def _claude_effort_provenance(
    *, model: str, env: Mapping[str, str], read_user_settings: bool
) -> dict:
    """Resolve Claude effort metadata without recording unrelated user settings."""
    if "haiku-4-5" in model.lower():
        return {
            "effort_level": None,
            "effort_supported": False,
            "effort_source": "unsupported_by_model",
        }

    for variable in ("CLINGEN_CLAUDE_EFFORT", "CLAUDE_CODE_EFFORT_LEVEL"):
        level = env.get(variable, "").strip().lower()
        if level:
            return {
                "effort_level": level,
                "effort_supported": True,
                "effort_source": variable,
            }

    # Claude Code also accepts effortLevel in its user settings. Read only that
    # single non-secret field so the manifest records the effective configuration.
    if read_user_settings:
        config_dir_value = env.get("CLAUDE_CONFIG_DIR", "").strip()
        config_dir = (Path(config_dir_value).expanduser() if config_dir_value
                      else Path.home() / ".claude")
        settings_path = config_dir / "settings.json"
        try:
            user_settings = json.loads(settings_path.read_text())
            level = str(user_settings.get("effortLevel", "")).strip().lower()
        except (OSError, ValueError, TypeError):
            level = ""
        if level:
            return {
                "effort_level": level,
                "effort_supported": True,
                "effort_source": "claude_user_settings",
            }

    return {
        "effort_level": None,
        "effort_supported": True,
        "effort_source": "model_default_unpinned",
    }


def _agent_provenance(agent_cmd: str, environ: Optional[dict[str, str]] = None) -> dict:
    """Capture reproducibility metadata from a strict, non-secret allow-list.

    For supported Claude Code and Codex adapters, also resolve effective model
    configuration from their explicit environment and non-secret config layers.
    """
    env = os.environ if environ is None else environ
    explicit_env = {
        name: env[name]
        for name in _SAFE_AGENT_ENV_VARS
        if env.get(name, "").strip()
    }
    provenance: dict = {"environment": explicit_env}

    codex = _codex_provenance(
        agent_cmd=agent_cmd,
        env=env,
        read_user_config=environ is None or bool(env.get("CODEX_HOME", "").strip()),
    )
    if codex is not None:
        provenance.update(codex)
        return provenance

    if "adapters/claude_code/" not in agent_cmd:
        return provenance

    use_vertex = env.get("CLAUDE_CODE_USE_VERTEX", "1") == "1"
    model = env.get("CLINGEN_CLAUDE_MODEL", "claude-opus-4-8")
    provenance.update({
        "adapter": "claude_code",
        "provider": "google_vertex_ai" if use_vertex else "anthropic_api",
        "model": model,
        "model_source": (
            "CLINGEN_CLAUDE_MODEL"
            if env.get("CLINGEN_CLAUDE_MODEL", "").strip()
            else "adapter_default"
        ),
    })
    provenance.update(_claude_effort_provenance(
        model=model,
        env=env,
        read_user_settings=environ is None or bool(env.get("CLAUDE_CONFIG_DIR", "").strip()),
    ))

    if use_vertex:
        # Claude Code gives the Google project variables precedence over
        # ANTHROPIC_VERTEX_PROJECT_ID. Mirror that ordering in the manifest.
        project_candidates = (
            "GOOGLE_CLOUD_PROJECT",
            "GCLOUD_PROJECT",
            "ANTHROPIC_VERTEX_PROJECT_ID",
        )
        project_source = next(
            (name for name in project_candidates if env.get(name, "").strip()),
            None,
        )
        provenance["project_id"] = (
            env[project_source] if project_source else "kehllab-caia-v2"
        )
        provenance["project_id_source"] = project_source or "adapter_default"
        region = env.get("CLOUD_ML_REGION", "").strip()
        provenance["region"] = region or None
        provenance["region_source"] = "CLOUD_ML_REGION" if region else "unknown"

    return provenance


def _serialise(inv: StageInvocation) -> dict:
    d = asdict(inv)
    # stdout/stderr already persisted to disk; keep size-bounded copies in JSON
    d["stdout"] = (d.get("stdout") or "")[:1000]
    d["stderr"] = (d.get("stderr") or "")[:2000]
    return d


def _per_question_dir(run_dir: Path, cohort: str, qid: str) -> Path:
    return run_dir / "per_question" / cohort / qid


def _build_question_payload(*, q: PublicQuestion, cohort: Cohort, stage: str, scratch_dir: Path,
                            data_dictionary_path: Path,
                            prior_classification: Optional[str] = None,
                            max_runtime_seconds: Optional[int] = None) -> dict:
    payload = {
        "contract_version": "2",
        "question_id": q.id,
        "question_text": q.text,
        "cohort": cohort.name,
        "category": q.category,
        "stage": stage,
        "cohort_dir": str(cohort.path.resolve()),
        "data_dictionary_path": str(data_dictionary_path.resolve()),
        "scratch_dir": str(scratch_dir.resolve()),
        "instructions": _DEFAULT_INSTRUCTIONS[stage],
        "disambiguation_concept_menu": concept_menu_payload(),
    }
    if prior_classification is not None:
        payload["prior_classification"] = prior_classification
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    return payload


def _run_one_question(*, agent_cmd: str, q: PublicQuestion, cohort: Cohort, run_dir: Path,
                      stages: list[str], agent_max_attempts: int,
                      agent_retry_base_seconds: float,
                      gold_classification: Optional[str] = None,
                      agent_env: Optional[Mapping[str, str]] = None,
                      timeout_config: Optional[TimeoutConfig] = None) -> QuestionRun:
    from ..cohorts import find_data_dictionary
    qdir = _per_question_dir(run_dir, cohort.name, q.id)
    qdir.mkdir(parents=True, exist_ok=True)
    scratch = qdir / "scratch"
    scratch.mkdir(exist_ok=True)
    dict_path = find_data_dictionary(cohort)
    timeouts = timeout_config or settings().timeouts
    qrun = QuestionRun(
        question_id=q.id, cohort=cohort.name, category=q.category,
        # runs/ lives in the agent-reachable repo, so keep it gold-free: only
        # populated for partial reruns that skip classify (harness-side gold).
        classification_gold=gold_classification or "",
    )

    classification: Optional[str] = None
    try:
        if "classify" in stages:
            payload = _build_question_payload(
                q=q, cohort=cohort, stage="classify", scratch_dir=scratch,
                data_dictionary_path=dict_path, max_runtime_seconds=timeouts.classify,
            )
            inv = invoke(
                agent_cmd=agent_cmd,
                question_payload=payload,
                question_path=qdir / "classify.question.json",
                result_path=qdir / "classify.json",
                stderr_log_path=qdir / "classify.agent.log",
                timeout_s=timeouts.classify,
                max_attempts=agent_max_attempts,
                retry_base_seconds=agent_retry_base_seconds,
                agent_env=agent_env,
            )
            qrun.classify = _serialise(inv)
            if inv.success and isinstance(inv.result, dict):
                classification = inv.result.get("classification")
        else:
            classification = gold_classification  # follow gold, useful for partial reruns

        if classification == "ambiguous" and "disambiguate" in stages:
            payload = _build_question_payload(
                q=q, cohort=cohort, stage="disambiguate", scratch_dir=scratch,
                data_dictionary_path=dict_path, prior_classification="ambiguous",
                max_runtime_seconds=timeouts.disambiguate,
            )
            inv = invoke(
                agent_cmd=agent_cmd,
                question_payload=payload,
                question_path=qdir / "disambiguate.question.json",
                result_path=qdir / "disambiguate.json",
                stderr_log_path=qdir / "disambiguate.agent.log",
                timeout_s=timeouts.disambiguate,
                max_attempts=agent_max_attempts,
                retry_base_seconds=agent_retry_base_seconds,
                agent_env=agent_env,
            )
            qrun.disambiguate = _serialise(inv)
        elif classification == "unambiguous" and "analyze" in stages:
            payload = _build_question_payload(
                q=q, cohort=cohort, stage="analyze", scratch_dir=scratch,
                data_dictionary_path=dict_path, prior_classification="unambiguous",
                max_runtime_seconds=timeouts.analyze,
            )
            inv = invoke(
                agent_cmd=agent_cmd,
                question_payload=payload,
                question_path=qdir / "analyze.question.json",
                result_path=qdir / "analyze.json",
                stderr_log_path=qdir / "analyze.agent.log",
                timeout_s=timeouts.analyze,
                max_attempts=agent_max_attempts,
                retry_base_seconds=agent_retry_base_seconds,
                agent_env=agent_env,
            )
            qrun.analyze = _serialise(inv)
    except Exception as exc:
        logger.exception("Question %s blew up", q.id)
        qrun.error = repr(exc)
    return qrun


def run_eval(
    *,
    agent_cmd: str,
    agent_name: str,
    cohort_spec: str = "all",
    question_id: Optional[str] = None,
    stages: Optional[list[str]] = None,
    max_parallel: int = 4,
    agent_max_attempts: int = 3,
    agent_retry_base_seconds: float = 5.0,
    run_id: Optional[str] = None,
) -> Path:
    """Run the harness against an agent. Returns the run directory."""
    stages = stages or ["classify", "disambiguate", "analyze"]
    if agent_max_attempts < 1:
        raise click_exception("agent_max_attempts must be >= 1")
    if agent_retry_base_seconds < 0:
        raise click_exception("agent_retry_base_seconds must be >= 0")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    run_dir = RUNS_DIR / agent_name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Run dir: %s", run_dir)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Collect questions from the PUBLIC (gold-free) bank — this is all the agent sees.
    cohorts = resolve_cohorts(cohort_spec)
    work: list[tuple[Cohort, PublicQuestion]] = []
    for c in cohorts:
        pcqf = q_io.load_public(c.name)
        if pcqf is None:
            logger.warning("No public questions YAML for %s; skipping.", c.name)
            continue
        for q in pcqf.questions:
            if question_id and q.id != question_id:
                continue
            work.append((c, q))

    n_total = len(work)
    if n_total == 0:
        raise click_exception("No questions matched the requested cohort/question filter.")

    # Partial reruns that skip the classify stage need the gold classification to
    # decide disambiguate-vs-analyze. Read it harness-side from the gold bank
    # (never exposed to the agent); normal full runs stay entirely gold-free.
    gold_cls: dict[tuple[str, str], str] = {}
    if "classify" not in stages:
        for c in cohorts:
            gcqf = q_io.load_gold(c.name)
            if gcqf is None:
                raise click_exception(
                    f"--stages omits 'classify' but no gold bank found for {c.name} "
                    f"(set CLINGEN_GOLD_ROOT / run sync)."
                )
            for q in gcqf.questions:
                gold_cls[(c.name, q.id)] = q.classification

    logger.info("Eval: %d question(s) across %d cohort(s); parallel=%d",
                n_total, len(cohorts), max_parallel)

    runs: list[QuestionRun] = []
    if max_parallel <= 1:
        for c, q in work:
            runs.append(_run_one_question(
                agent_cmd=agent_cmd, q=q, cohort=c, run_dir=run_dir, stages=stages,
                agent_max_attempts=agent_max_attempts,
                agent_retry_base_seconds=agent_retry_base_seconds,
                gold_classification=gold_cls.get((c.name, q.id)),
            ))
    else:
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = [pool.submit(_run_one_question, agent_cmd=agent_cmd, q=q, cohort=c,
                                   run_dir=run_dir, stages=stages,
                                   agent_max_attempts=agent_max_attempts,
                                   agent_retry_base_seconds=agent_retry_base_seconds,
                                   gold_classification=gold_cls.get((c.name, q.id)))
                       for c, q in work]
            for fut in as_completed(futures):
                runs.append(fut.result())

    ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = RunManifest(
        agent_cmd=agent_cmd,
        agent_name=agent_name,
        run_id=run_id,
        started_at=started,
        ended_at=ended,
        host=socket.gethostname(),
        platform=platform.platform(),
        cohorts=[c.name for c in cohorts],
        n_questions=n_total,
        n_completed=sum(1 for r in runs if r.error is None),
        agent_provenance=_agent_provenance(agent_cmd),
        settings={
            "stages": stages,
            "max_parallel": max_parallel,
            "timeouts": asdict(settings().timeouts),
            "retries": {
                "max_attempts": agent_max_attempts,
                "base_delay_seconds": agent_retry_base_seconds,
                "backoff": "exponential",
            },
        },
    )
    atomic_write_json(run_dir / "manifest.json", asdict(manifest))
    atomic_write_json(run_dir / "runs.json", [asdict(r) for r in runs])
    return run_dir


def click_exception(msg: str) -> Exception:
    """Avoid importing click in the orchestrator core, but produce a useful error."""
    return RuntimeError(msg)
