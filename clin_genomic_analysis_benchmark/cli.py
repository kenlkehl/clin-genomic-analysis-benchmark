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
@click.option("--agent", required=True,
              help='CLI command (e.g. "bash adapters/claude_code/run.sh").')
@click.option("--agent-name", required=True)
@click.option("--cohort", default="all", show_default=True)
@click.option("--question", default=None,
              help="Restrict to a single question id (across cohorts).")
@click.option("--stages", default="classify,disambiguate,analyze", show_default=True)
@click.option("--max-parallel", default=4, type=int, show_default=True)
@click.option("--agent-max-attempts", default=3, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum harness attempts for each failed agent stage.")
@click.option("--agent-retry-base-seconds", default=5.0,
              type=click.FloatRange(min=0), show_default=True,
              help="Initial retry delay; subsequent delays use exponential backoff.")
@click.option("--retry-failures/--no-retry-failures", default=True, show_default=True,
              help="After eval, repair score-relevant technical failures in a copied run.")
@click.option("--repair-max-passes", default=10, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum automatic repair passes after the evaluation.")
@click.option("--repair-agent-max-attempts", default=10,
              type=click.IntRange(min=1), show_default=True,
              help="Maximum attempts per failed stage in each automatic repair pass.")
@click.option("--repair-max-parallel", default=2, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum concurrent calls during automatic repair.")
@click.option("--repair-classify-timeout", default=None, type=click.IntRange(min=1),
              help="Override the classification timeout during repair, in seconds.")
@click.option("--repair-disambiguate-timeout", default=None,
              type=click.IntRange(min=1),
              help="Override the disambiguation timeout during repair, in seconds.")
@click.option("--repair-analyze-timeout", default=None, type=click.IntRange(min=1),
              help="Override the analysis timeout during repair, in seconds.")
@click.option("--run-id", default=None)
@click.option("--verbose", is_flag=True)
def eval(agent: str, agent_name: str, cohort: str, question: str | None,
         stages: str, max_parallel: int, agent_max_attempts: int,
         agent_retry_base_seconds: float, retry_failures: bool,
         repair_max_passes: int, repair_agent_max_attempts: int,
         repair_max_parallel: int,
         repair_classify_timeout: int | None,
         repair_disambiguate_timeout: int | None,
         repair_analyze_timeout: int | None,
         run_id: str | None, verbose: bool) -> None:
    """Evaluate an agent against the benchmark (Phase D)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .agent.isolation import AgentIsolationError
    from .agent.orchestrator import run_eval

    stages_list = [s.strip() for s in stages.split(",") if s.strip()]
    try:
        run_dir = run_eval(
            agent_cmd=agent,
            agent_name=agent_name,
            cohort_spec=cohort,
            question_id=question,
            stages=stages_list,
            max_parallel=max_parallel,
            agent_max_attempts=agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            run_id=run_id,
        )
    except AgentIsolationError as exc:
        raise click.ClickException(f"agent isolation failed: {exc}") from exc
    click.echo(f"Eval written to {run_dir}")
    from .config import SCORING_CONFIG_DIR
    from .scoring.driver import score_run

    cfg_path = SCORING_CONFIG_DIR / "default.yaml"
    if not cfg_path.exists():
        cfg_path = None

    def score_completed_run(path: Path) -> None:
        try:
            scored_dir = score_run(
                run_path=str(path),
                scoring_config_path=cfg_path,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise click.ClickException(
                f"Eval completed at {path}, but automatic scoring failed: {exc}"
            ) from exc
        click.echo(f"Scorecard: {scored_dir / 'scorecard.md'}")

    if not retry_failures:
        score_completed_run(run_dir)
        return

    from .agent.repair import plan_failed_run, retry_failed_run_until_stable

    try:
        _, repair_targets = plan_failed_run(run_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(
            f"Eval completed at {run_dir}, but post-run repair planning failed: {exc}"
        ) from exc
    if not repair_targets:
        click.echo("Post-run repair: no score-relevant technical failures.")
        score_completed_run(run_dir)
        return

    click.echo(f"Post-run repair: retrying {len(repair_targets)} failed question(s).")
    try:
        timeout_overrides = {
            stage: value
            for stage, value in {
                "classify": repair_classify_timeout,
                "disambiguate": repair_disambiguate_timeout,
                "analyze": repair_analyze_timeout,
            }.items()
            if value is not None
        }
        loop = retry_failed_run_until_stable(
            run_path=run_dir,
            agent_cmd=agent,
            max_parallel=repair_max_parallel,
            agent_max_attempts=repair_agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            timeout_overrides=timeout_overrides,
            max_repair_passes=repair_max_passes,
            scoring_config_path=cfg_path,
        )
    except (AgentIsolationError, FileNotFoundError, FileExistsError, ValueError) as exc:
        raise click.ClickException(
            f"Eval completed at {run_dir}, but post-run repair failed: {exc}"
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


@cli.command("retry-failures")
@click.option("--run", required=True,
              help="Source run relative under runs/ or an absolute run directory.")
@click.option("--output-run-id", default=None,
              help="Directory name for the repaired copy (default: generated).")
@click.option("--agent", default=None,
              help="Override the source manifest's agent command.")
@click.option("--max-parallel", default=2, type=click.IntRange(min=1), show_default=True)
@click.option("--agent-max-attempts", default=10, type=click.IntRange(min=1),
              show_default=True,
              help="Maximum attempts per selected failed stage in each repair pass.")
@click.option("--agent-retry-base-seconds", default=5.0,
              type=click.FloatRange(min=0), show_default=True,
              help="Initial retry delay; subsequent delays use exponential backoff.")
@click.option("--max-repair-passes", default=10, type=click.IntRange(min=1),
              show_default=True,
              help="Stop after this many repair passes even if failures remain.")
@click.option("--classify-timeout", default=None, type=click.IntRange(min=1),
              help="Override classification timeout during repair, in seconds.")
@click.option("--disambiguate-timeout", default=None, type=click.IntRange(min=1),
              help="Override disambiguation timeout during repair, in seconds.")
@click.option("--analyze-timeout", default=None, type=click.IntRange(min=1),
              help="Override analysis timeout during repair, in seconds.")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Scoring config YAML (defaults to scoring_configs/default.yaml).")
@click.option("--dry-run", is_flag=True,
              help="List score-relevant failures without executing the agent or writing files.")
@click.option("--verbose", is_flag=True)
def retry_failures(run: str, output_run_id: str | None, agent: str | None,
                   max_parallel: int, agent_max_attempts: int,
                   agent_retry_base_seconds: float, max_repair_passes: int,
                   classify_timeout: int | None, disambiguate_timeout: int | None,
                   analyze_timeout: int | None, config: str | None,
                   dry_run: bool, verbose: bool) -> None:
    """Retry technical failures in a copied run and regenerate its scorecard."""
    import logging as _logging

    from .agent.isolation import AgentIsolationError
    from .agent.repair import plan_failed_run, retry_failed_run_until_stable
    from .config import SCORING_CONFIG_DIR

    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        source_dir, targets = plan_failed_run(run)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Source run: {source_dir}")
    click.echo(f"Score-relevant technical failures: {len(targets)}")
    for target in targets:
        click.echo(
            f"  - {target.cohort}/{target.question_id}: {target.stage} "
            f"({target.original_failure_reason})"
        )
    if dry_run:
        return
    if not targets:
        click.echo("Nothing to retry; source run left unchanged.")
        return

    cfg_path = Path(config) if config else (SCORING_CONFIG_DIR / "default.yaml")
    if not cfg_path.exists():
        cfg_path = None
    try:
        timeout_overrides = {
            stage: value
            for stage, value in {
                "classify": classify_timeout,
                "disambiguate": disambiguate_timeout,
                "analyze": analyze_timeout,
            }.items()
            if value is not None
        }
        loop = retry_failed_run_until_stable(
            run_path=run,
            output_run_id=output_run_id,
            agent_cmd=agent,
            max_parallel=max_parallel,
            agent_max_attempts=agent_max_attempts,
            agent_retry_base_seconds=agent_retry_base_seconds,
            timeout_overrides=timeout_overrides,
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


@cli.command()
@click.option("--run", required=True,
              help="Run dir: relative under runs/ (e.g. claude_code/20260422T...) or absolute.")
@click.option("--config", default=None, type=click.Path(exists=True),
              help="Path to scoring config YAML (defaults to scoring_configs/default.yaml if present).")
@click.option("--verbose", is_flag=True)
def score(run: str, config: str | None, verbose: bool) -> None:
    """Score a run (Phase E)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO if verbose else _logging.WARNING,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from pathlib import Path as _P
    from .scoring.driver import score_run
    from .config import SCORING_CONFIG_DIR

    cfg_path = _P(config) if config else (SCORING_CONFIG_DIR / "default.yaml")
    if not cfg_path.exists():
        cfg_path = None  # use built-in defaults
    try:
        out = score_run(run_path=run, scoring_config_path=cfg_path)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Scorecard written to {out / 'scorecard.md'}")


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
    from .config import SCORING_CONFIG_DIR
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
    cfg_path = _P(config) if config else (SCORING_CONFIG_DIR / "default.yaml")
    if not cfg_path.exists():
        cfg_path = None
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
