"""clingen-bench CLI."""

from __future__ import annotations

from pathlib import Path

import click

from . import cohorts as _cohorts
from . import data_dictionary, sampling
from .config import ensure_dirs


@click.group()
def cli():
    """clin-genomic-analysis-benchmark: AI-agent translational cancer-data analysis benchmark."""
    ensure_dirs()


@cli.command()
@click.option("--cohort", required=True, help="Cohort name (or 'all').")
@click.option("--rebuild", is_flag=True, help="Force re-parse of dictionary and re-sampling.")
@click.option("--max-cols-listed", default=0, type=int, help="Truncate per-table column list (0=all).")
def inspect(cohort: str, rebuild: bool, max_cols_listed: int) -> None:
    """Print dictionary + cohort context summary for a cohort (sanity check)."""
    for c in _cohorts.resolve_cohorts(cohort):
        click.echo(f"\n========== {c.name} ==========")
        click.echo(f"Path: {c.path}")
        if not c.path.exists():
            click.echo("  (missing)", err=True)
            continue

        variables = data_dictionary.load(c, use_cache=not rebuild)
        click.echo(f"\nDictionary: {len(variables)} variables across "
                   f"{len({v.dataset for v in variables})} datasets")
        for ds in sorted({v.dataset for v in variables}):
            n = sum(1 for v in variables if v.dataset == ds)
            click.echo(f"  - {ds}: {n} vars")

        ctx = sampling.build(c, use_cache=not rebuild)
        click.echo("\nFiles by category:")
        for cat, names in ctx.files_by_category.items():
            if names:
                click.echo(f"  - {cat}: {len(names)} file(s)")
        click.echo(f"\nTabular files summarised: {len(ctx.tables)}")
        for fn in sorted(ctx.tables)[:5]:
            t = ctx.tables[fn]
            click.echo(f"  - {fn}: {t.n_columns} cols, sampled {t.n_rows_sampled} rows")


@cli.command("dump-context")
@click.option("--cohort", required=True, help="Cohort name.")
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path),
              help="Output Markdown path for the cohort context.")
def dump_context(cohort: str, output: Path) -> None:
    """Dump the cohort context (dictionary + table summaries) as Markdown."""
    c = _cohorts.get_cohort(cohort)
    variables = data_dictionary.load(c)
    ctx = sampling.build(c)
    output.parent.mkdir(parents=True, exist_ok=True)
    md = "# Data dictionary\n" + data_dictionary.to_compact_markdown(variables) \
         + "\n\n" + sampling.to_compact_markdown(ctx)
    output.write_text(md)
    click.echo(f"Wrote {output} ({len(md):,} chars)")


# --- Stubs to be filled in by Phases B–E ---

@cli.command("generate-questions")
@click.option("--cohort", required=True, help="Cohort name (or 'all').")
@click.option("--n-per-category", default=5, type=int, show_default=True)
@click.option("--target-ambiguous-frac", default=0.5, type=float, show_default=True)
@click.option("--category", "only_category", default=None, type=int,
              help="Generate only one category id (1-8). For dev / smoke testing.")
@click.option("--force", is_flag=True,
              help="Re-generate even already-reviewed questions (DESTRUCTIVE).")
@click.option("--verbose", is_flag=True)
def generate_questions(cohort: str, n_per_category: int, target_ambiguous_frac: float,
                       only_category: int | None, force: bool, verbose: bool) -> None:
    """Generate per-cohort question YAML (Phase B)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .questions.generator import generate_for_cohort

    only_cats = [only_category] if only_category else None
    for c in _cohorts.resolve_cohorts(cohort):
        click.echo(f"\n>>> {c.name}: generating {n_per_category} questions per category"
                   f"{' (cat ' + str(only_category) + ' only)' if only_category else ''}...")
        path = generate_for_cohort(
            cohort=c,
            n_per_category=n_per_category,
            target_ambiguous_frac=target_ambiguous_frac,
            only_categories=only_cats,
            force=force,
        )
        click.echo(f"    -> {path}")


@cli.command("compute-gold")
@click.option("--cohort", required=True, help="Cohort name (or 'all').")
@click.option("--max-repair-iters", default=3, type=int, show_default=True)
@click.option("--only", default=None, help="Run a single question id (e.g. bladder_1.2-Qabc12345).")
@click.option("--only-file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="File listing 'cohort/question_id' per line; forces recompute even if gold_answer exists.")
@click.option("--sandbox-timeout-s", default=300, type=int, show_default=True)
@click.option("--verbose", is_flag=True)
def compute_gold(cohort: str, max_repair_iters: int, only: str | None,
                 only_file: str | None,
                 sandbox_timeout_s: int, verbose: bool) -> None:
    """Compute gold-standard answers for unambiguous questions (Phase C)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .gold_standard.runner import compute_for_cohort

    per_cohort_only: dict[str, set[str]] = {}
    if only_file:
        with open(only_file) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "/" not in line:
                    raise click.BadParameter(
                        f"--only-file lines must be 'cohort/qid'; got {line!r}"
                    )
                coh, qid = line.split("/", 1)
                per_cohort_only.setdefault(coh, set()).add(qid)

    for c in _cohorts.resolve_cohorts(cohort):
        only_qids = per_cohort_only.get(c.name) if only_file else None
        if only_file and not only_qids:
            click.echo(f"\n>>> {c.name}: no question ids in --only-file; skipping.")
            continue
        scope = (
            f" (only {only})" if only
            else (f" (only {len(only_qids)} from --only-file)" if only_qids else "")
        )
        click.echo(f"\n>>> {c.name}: computing gold-standard answers{scope}...")
        cqf, outcomes = compute_for_cohort(
            cohort=c, only_qid=only, only_qids=only_qids,
            max_repair_iters=max_repair_iters,
            sandbox_timeout_s=sandbox_timeout_s,
        )
        n_ok = sum(1 for o in outcomes if o.success)
        click.echo(f"    {n_ok}/{len(outcomes)} succeeded")
        for o in outcomes:
            tag = "OK" if o.success else "FAIL"
            click.echo(f"    [{tag}] {o.question_id} ({o.repair_attempts + 1} attempts, "
                       f"{o.duration_seconds:.1f}s)" + (f"  reason={o.failure_reason}" if not o.success else ""))


@cli.command()
@click.option("--model", "model_spec", required=True,
              help="<provider>:<model>, e.g. vertex-anthropic:claude-opus-5-5, "
                   "vertex-openai:google/gemini-3-pro, "
                   "vertex-responses:meta/muse-ember-1.0, azure-openai:gpt-5, "
                   "openai-compatible:google/gemma-4-26b-it.")
@click.option("--run-name", default=None,
              help="Directory under runs/ (default: assess_<model>).")
@click.option("--cohort", default="all", show_default=True)
@click.option("--question", default=None,
              help="Restrict to a single question id (across cohorts).")
@click.option("--max-parallel", default=4, type=click.IntRange(min=1), show_default=True)
@click.option("--max-attempts", default=3, type=click.IntRange(min=1), show_default=True,
              help="Calls per question until a reply validates.")
@click.option("--retry-base-seconds", default=5.0, type=click.FloatRange(min=0),
              show_default=True,
              help="Initial retry delay; subsequent delays use exponential backoff.")
@click.option("--max-tokens", default=None, type=click.IntRange(min=1),
              help="Reply token limit (default: 32000 for vertex-anthropic, server default "
                   "otherwise).")
@click.option("--temperature", default=None, type=float)
@click.option("--params", default=None,
              help='Extra request fields as JSON, e.g. \'{"reasoning_effort": "high"}\'.')
@click.option("--base-url", default=None,
              help="Endpoint for openai-compatible (or an Azure v1 endpoint override).")
@click.option("--api-key-env", default=None,
              help="Environment variable holding the openai-compatible API key.")
@click.option("--project", default=None, help="Google Cloud project for vertex-* providers.")
@click.option("--region", default=None, help="Vertex region (default: global).")
@click.option("--request-timeout", default=900.0, type=click.FloatRange(min=1),
              show_default=True, help="Seconds before one API call is abandoned.")
@click.option("--run-id", default=None)
@click.option("--verbose", is_flag=True)
def assess(model_spec: str, run_name: str | None, cohort: str, question: str | None,
           max_parallel: int, max_attempts: int, retry_base_seconds: float,
           max_tokens: int | None, temperature: float | None, params: str | None,
           base_url: str | None, api_key_env: str | None, project: str | None,
           region: str | None, request_timeout: float, run_id: str | None,
           verbose: bool) -> None:
    """Run the assess task: one prompt per question, no tools, then score it."""
    import json as _json
    import logging as _logging

    from .assess.backends import BackendConfig, parse_model_spec
    from .assess.runner import default_run_name, run_assess

    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        provider, model = parse_model_spec(model_spec)
        extra = _json.loads(params) if params else {}
        if not isinstance(extra, dict):
            raise ValueError("--params must be a JSON object")
        config = BackendConfig(
            provider=provider, model=model, max_tokens=max_tokens, temperature=temperature,
            params=extra, base_url=base_url, api_key_env=api_key_env, project=project,
            region=region, request_timeout_seconds=request_timeout,
        )
        run_dir = run_assess(
            backend_config=config, run_name=run_name or default_run_name(model_spec),
            cohort_spec=cohort, question_id=question, max_parallel=max_parallel,
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds, run_id=run_id,
        )
    except (ValueError, _json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Assess run written to {run_dir}")
    _score_or_fail(run_dir)


def _default_scoring_config() -> Path | None:
    from .config import SCORING_CONFIG_DIR

    path = SCORING_CONFIG_DIR / "default.yaml"
    return path if path.exists() else None


def _score_or_fail(run_dir: Path) -> None:
    from .scoring.driver import score_run

    try:
        scored_dir = score_run(run_path=str(run_dir),
                               scoring_config_path=_default_scoring_config())
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(
            f"Run completed at {run_dir}, but automatic scoring failed: {exc}") from exc
    click.echo(f"Scorecard: {scored_dir / 'scorecard.md'}")


@cli.command()
@click.option("--agent", required=True,
              help='CLI command (e.g. "bash adapters/claude_code/run.sh").')
@click.option("--agent-name", required=True)
@click.option("--cohort", default="all", show_default=True)
@click.option("--question", default=None,
              help="Restrict to a single gold-unambiguous question id (across cohorts).")
@click.option("--max-parallel", default=4, type=int, show_default=True)
@click.option("--agent-max-attempts", default=3, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum harness attempts for each failed question.")
@click.option("--agent-retry-base-seconds", default=5.0,
              type=click.FloatRange(min=0), show_default=True,
              help="Initial retry delay; subsequent delays use exponential backoff.")
@click.option("--retry-failures/--no-retry-failures", default=True, show_default=True,
              help="After the run, repair score-relevant technical failures in a copied run.")
@click.option("--repair-max-passes", default=10, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum automatic repair passes after the run.")
@click.option("--repair-agent-max-attempts", default=10,
              type=click.IntRange(min=1), show_default=True,
              help="Maximum attempts per failed question in each automatic repair pass.")
@click.option("--repair-max-parallel", default=2, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum concurrent calls during automatic repair.")
@click.option("--repair-analyze-timeout", default=None, type=click.IntRange(min=1),
              help="Override the analysis timeout during repair, in seconds.")
@click.option("--run-id", default=None)
@click.option("--verbose", is_flag=True)
def analyze(agent: str, agent_name: str, cohort: str, question: str | None,
            max_parallel: int, agent_max_attempts: int,
            agent_retry_base_seconds: float, retry_failures: bool,
            repair_max_passes: int, repair_agent_max_attempts: int,
            repair_max_parallel: int, repair_analyze_timeout: int | None,
            run_id: str | None, verbose: bool) -> None:
    """Run the analyze task: an agent computes every gold-unambiguous question."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .agent.isolation import AgentIsolationError
    from .agent.orchestrator import run_analyze

    try:
        run_dir = run_analyze(
            agent_cmd=agent,
            agent_name=agent_name,
            cohort_spec=cohort,
            question_id=question,
            max_parallel=max_parallel,
            agent_max_attempts=agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            run_id=run_id,
        )
    except AgentIsolationError as exc:
        raise click.ClickException(f"agent isolation failed: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Analyze run written to {run_dir}")

    if not retry_failures:
        _score_or_fail(run_dir)
        return

    from .agent.repair import plan_failed_run, retry_failed_run_until_stable

    try:
        _, repair_targets = plan_failed_run(run_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(
            f"Run completed at {run_dir}, but post-run repair planning failed: {exc}"
        ) from exc
    if not repair_targets:
        click.echo("Post-run repair: no score-relevant technical failures.")
        _score_or_fail(run_dir)
        return

    click.echo(f"Post-run repair: retrying {len(repair_targets)} failed question(s).")
    try:
        loop = retry_failed_run_until_stable(
            run_path=run_dir,
            agent_cmd=agent,
            max_parallel=repair_max_parallel,
            agent_max_attempts=repair_agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            timeout_overrides=(
                {"analyze": repair_analyze_timeout} if repair_analyze_timeout else {}),
            max_repair_passes=repair_max_passes,
            scoring_config_path=_default_scoring_config(),
        )
    except (AgentIsolationError, FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(
            f"Run completed at {run_dir}, but post-run repair failed: {exc}"
        ) from exc
    for index, summary in enumerate(loop.passes, start=1):
        click.echo(
            f"Post-run repair pass {index} merged "
            f"{summary.successful_merges}/{len(summary.targets)} successful retries."
        )
    click.echo(
        f"Post-run repair stopped: {loop.stop_reason}; "
        f"{len(loop.remaining_targets)} score-relevant failure(s) remain."
    )
    click.echo(f"Final repaired run: {loop.final_run_dir}")
    click.echo(f"Scorecard: {loop.final_run_dir / 'scorecard.md'}")


def _run_task(run: str) -> str:
    import json as _json

    from .scoring.driver import resolve_run_dir

    try:
        manifest = _json.loads((resolve_run_dir(run) / "manifest.json").read_text())
    except (FileNotFoundError, _json.JSONDecodeError) as exc:
        raise click.ClickException(f"cannot read the run manifest: {exc}") from exc
    return str(manifest.get("task") or "")


@cli.command("retry-failures")
@click.option("--run", required=True,
              help="Source run relative under runs/ or an absolute run directory.")
@click.option("--output-run-id", default=None,
              help="Directory name for the repaired copy (default: generated).")
@click.option("--agent", default=None,
              help="Analyze runs: override the source manifest's agent command.")
@click.option("--max-parallel", default=2, type=click.IntRange(min=1), show_default=True)
@click.option("--max-attempts", "--agent-max-attempts", "max_attempts", default=None,
              type=click.IntRange(min=1),
              help="Attempts per failed question in each pass (default: 10 for analyze "
                   "runs, 5 for assess runs).")
@click.option("--agent-retry-base-seconds", "--retry-base-seconds", "retry_base_seconds",
              default=5.0, type=click.FloatRange(min=0), show_default=True,
              help="Initial retry delay; subsequent delays use exponential backoff.")
@click.option("--max-repair-passes", default=10, type=click.IntRange(min=1),
              show_default=True,
              help="Analyze runs: stop after this many repair passes even if failures remain.")
@click.option("--analyze-timeout", default=None, type=click.IntRange(min=1),
              help="Analyze runs: override the analysis timeout during repair, in seconds.")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Scoring config YAML (defaults to scoring_configs/default.yaml).")
@click.option("--dry-run", is_flag=True,
              help="List score-relevant failures without calling a model or writing files.")
@click.option("--verbose", is_flag=True)
def retry_failures(run: str, output_run_id: str | None, agent: str | None,
                   max_parallel: int, max_attempts: int | None,
                   retry_base_seconds: float, max_repair_passes: int,
                   analyze_timeout: int | None, config: str | None,
                   dry_run: bool, verbose: bool) -> None:
    """Retry technical failures of an assess or analyze run in a copy, then rescore it."""
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg_path = Path(config) if config else _default_scoring_config()
    task = _run_task(run)
    if task == "assess":
        _retry_assess(run=run, output_run_id=output_run_id, agent=agent,
                      max_parallel=max_parallel, max_attempts=max_attempts or 5,
                      retry_base_seconds=retry_base_seconds, analyze_timeout=analyze_timeout,
                      cfg_path=cfg_path, dry_run=dry_run)
        return
    if task != "analyze":
        raise click.ClickException(
            f"{run} has no assess/analyze task in its manifest; runs from before the "
            "assess/analyze split cannot be repaired by this version")

    from .agent.isolation import AgentIsolationError
    from .agent.repair import plan_failed_run, retry_failed_run_until_stable

    try:
        source_dir, targets = plan_failed_run(run)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Source run: {source_dir}")
    click.echo(f"Score-relevant technical failures: {len(targets)}")
    for target in targets:
        click.echo(f"  - {target.cohort}/{target.question_id} "
                   f"({target.original_failure_reason})")
    if dry_run:
        return
    if not targets:
        click.echo("Nothing to retry; source run left unchanged.")
        return

    try:
        loop = retry_failed_run_until_stable(
            run_path=run,
            output_run_id=output_run_id,
            agent_cmd=agent,
            max_parallel=max_parallel,
            agent_max_attempts=max_attempts or 10,
            agent_retry_base_seconds=retry_base_seconds,
            timeout_overrides={"analyze": analyze_timeout} if analyze_timeout else {},
            max_repair_passes=max_repair_passes,
            scoring_config_path=cfg_path,
        )
    except (AgentIsolationError, FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    for index, summary in enumerate(loop.passes, start=1):
        click.echo(
            f"Pass {index}: merged {summary.successful_merges}/"
            f"{len(summary.targets)} successful retries; "
            f"{summary.failed_retries} failed this pass."
        )
    click.echo(
        f"Repair stopped: {loop.stop_reason}; "
        f"{len(loop.remaining_targets)} score-relevant failure(s) remain."
    )
    click.echo(f"Repaired run: {loop.final_run_dir}")
    click.echo(f"Scorecard: {loop.final_run_dir / 'scorecard.md'}")


def _retry_assess(*, run: str, output_run_id: str | None, agent: str | None,
                  max_parallel: int, max_attempts: int, retry_base_seconds: float,
                  analyze_timeout: int | None, cfg_path: Path | None, dry_run: bool) -> None:
    import json as _json

    from .assess.runner import failed_questions, retry_assess_failures
    from .scoring.driver import resolve_run_dir, score_run

    if agent or analyze_timeout:
        raise click.ClickException("--agent and --analyze-timeout apply only to analyze runs")
    source_dir = resolve_run_dir(run)
    targets = failed_questions(_json.loads((source_dir / "runs.json").read_text()))
    click.echo(f"Source run: {source_dir}")
    click.echo(f"Failed assess calls: {len(targets)}")
    for target in targets:
        reason = (target.get("assess") or {}).get("failure_reason") or target.get("error")
        click.echo(f"  - {target['cohort']}/{target['question_id']} ({reason})")
    if dry_run:
        return
    if not targets:
        click.echo("Nothing to retry; source run left unchanged.")
        return
    try:
        summary = retry_assess_failures(
            run_path=run, max_parallel=max_parallel, max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds, output_run_id=output_run_id)
        score_run(run_path=str(summary.retried_run_dir), scoring_config_path=cfg_path)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Recovered {summary.n_recovered}/{summary.n_targets} question(s).")
    click.echo(f"Retried run: {summary.retried_run_dir}")
    click.echo(f"Scorecard: {summary.retried_run_dir / 'scorecard.md'}")


@cli.command()
@click.option("--run", required=True,
              help="Run dir: relative under runs/ (e.g. claude_code/20260422T...) or absolute.")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Path to scoring config YAML (defaults to scoring_configs/default.yaml if present).")
@click.option("--verbose", is_flag=True)
def score(run: str, config: str | None, verbose: bool) -> None:
    """Score an assess or analyze run."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .scoring.driver import score_run

    cfg_path = Path(config) if config else _default_scoring_config()
    try:
        out = score_run(run_path=run, scoring_config_path=cfg_path)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Scorecard written to {out / 'scorecard.md'}")


@cli.command()
@click.option("--assess-run", required=True, help="A scored assess run (under runs/ or absolute).")
@click.option("--analyze-run", required=True, help="A scored analyze run (under runs/ or absolute).")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Scoring config YAML with subtask_weights "
                   "(defaults to scoring_configs/default.yaml).")
@click.option("--run-id", default=None, help="Directory name under runs/combined/.")
@click.option("--allow-mismatch", is_flag=True,
              help="Combine even if cohorts, gold bank, conventions, or coverage differ.")
def combine(assess_run: str, analyze_run: str, config: str | None, run_id: str | None,
            allow_mismatch: bool) -> None:
    """Weight one assess run and one analyze run into the headline score."""
    from .scoring.combine import combine_runs

    cfg_path = Path(config) if config else _default_scoring_config()
    try:
        out = combine_runs(assess_run=assess_run, analyze_run=analyze_run,
                           scoring_config_path=cfg_path, run_id=run_id,
                           allow_mismatch=allow_mismatch)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Combined scorecard written to {out / 'scorecard.md'}")


@cli.command("adjudicate-current-run-path-leak")
@click.option("--run", required=True,
              help="Quarantined run relative under runs/ or an absolute run directory.")
@click.option("--reviewer", required=True,
              help="Person or process responsible for the review decision.")
@click.option("--rationale", required=True,
              help="Why the limited path disclosure did not expose gold or prior-run content.")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Scoring config YAML (defaults to scoring_configs/default.yaml).")
@click.option("--score/--no-score", default=True, show_default=True,
              help="Generate deterministic scorecards after successful adjudication.")
def adjudicate_current_run_path_leak(run: str, reviewer: str, rationale: str,
                                    config: str | None, score: bool) -> None:
    """Accept a schema-v1 current-run procfs path leak under a narrow policy."""
    from pathlib import Path as _P

    from .agent.integrity import (
        IntegrityAdjudicationError,
        adjudicate_current_run_mount_path_leak,
    )
    from .scoring.driver import score_run

    try:
        run_dir, record = adjudicate_current_run_mount_path_leak(
            run_path=run,
            reviewer=reviewer,
            rationale=rationale,
        )
    except (FileNotFoundError, IntegrityAdjudicationError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Integrity adjudication recorded for {run_dir} "
        f"under {record['policy']}."
    )
    if not score:
        return
    cfg_path = _P(config) if config else _default_scoring_config()
    try:
        score_run(run_path=str(run_dir), scoring_config_path=cfg_path)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(
            f"adjudication was recorded, but scoring failed: {exc}"
        ) from exc
    click.echo(f"Scorecard written to {run_dir / 'scorecard.md'}")


@cli.command("init-adapter")
@click.option("--name", required=True)
def init_adapter(name: str) -> None:
    """Scaffold a new adapter directory (Phase F). Not yet implemented."""
    raise click.ClickException("init-adapter: implemented in Phase F")


if __name__ == "__main__":
    cli()
