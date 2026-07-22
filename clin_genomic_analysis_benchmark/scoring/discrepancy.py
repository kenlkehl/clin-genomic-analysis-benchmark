"""Per-answer-type discrepancy scoring.

Bands map to points:
  ACCURATE (≤5% rel diff / correct bucket / exact match) → 2 pts
  MINOR    (5–15% rel diff / off by one bucket)          → 1 pt
  MAJOR    (>15% rel diff / wrong bucket / wrong cat)    → 0 pts
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Band(Enum):
    ACCURATE = "accurate"
    MINOR = "minor"
    MAJOR = "major"


_POINTS = {Band.ACCURATE: 2, Band.MINOR: 1, Band.MAJOR: 0}


def points(band: Band) -> int:
    return _POINTS[band]


@dataclass
class DiscrepancyResult:
    band: Band
    points: int
    metric: str                # "relative_pct" | "log_ratio_pct" | "pvalue_bucket" | "exact_match"
    metric_value: Optional[float]
    explanation: str


# ----- thresholds (overridable via scoring_configs/default.yaml later) -----

_ACC_THRESH = 0.05
_MIN_THRESH = 0.15


def _safe_float(x) -> Optional[float]:
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def score_count(agent: dict, gold: dict, *, accurate_threshold: float = _ACC_THRESH,
                minor_threshold: float = _MIN_THRESH) -> DiscrepancyResult:
    return _score_relative(agent.get("value"), gold.get("value"),
                           accurate_threshold, minor_threshold)


def score_proportion(agent: dict, gold: dict, *, mode: str = "relative",
                     accurate_threshold: float = _ACC_THRESH,
                     minor_threshold: float = _MIN_THRESH) -> DiscrepancyResult:
    a = _safe_float(agent.get("value"))
    g = _safe_float(gold.get("value"))
    if a is None or g is None:
        return DiscrepancyResult(Band.MAJOR, 0, "relative_pct", None,
                                 f"missing value (agent={agent.get('value')!r}, gold={gold.get('value')!r})")
    if mode == "absolute_pp":
        diff = abs(a - g)
        # treat thresholds as absolute proportion points (e.g. 0.05 = 5 pp)
        band = (Band.ACCURATE if diff <= accurate_threshold
                else Band.MINOR if diff <= minor_threshold
                else Band.MAJOR)
        return DiscrepancyResult(band, _POINTS[band], "absolute_pp", diff,
                                 f"|{a:.4f} - {g:.4f}| = {diff:.4f}")
    # relative
    return _score_relative(a, g, accurate_threshold, minor_threshold)


def score_median_with_ci(agent: dict, gold: dict, *, accurate_threshold: float = _ACC_THRESH,
                         minor_threshold: float = _MIN_THRESH) -> DiscrepancyResult:
    """Score median by relative %."""
    return _score_relative(agent.get("value"), gold.get("value"),
                           accurate_threshold, minor_threshold)


def score_ratio_log(agent: dict, gold: dict, *, accurate_threshold: float = _ACC_THRESH,
                    minor_threshold: float = _MIN_THRESH) -> DiscrepancyResult:
    """Score HR/OR by relative % difference on the log scale.

    metric = |log(a) - log(g)| / |log(g)|, clamped at 1.0 when log(g) is near 0.
    """
    a = _safe_float(agent.get("value"))
    g = _safe_float(gold.get("value"))
    if a is None or g is None or a <= 0 or g <= 0:
        return DiscrepancyResult(Band.MAJOR, 0, "log_ratio_pct", None,
                                 f"missing or non-positive value (agent={agent.get('value')!r}, gold={gold.get('value')!r})")
    log_a = math.log(a)
    log_g = math.log(g)
    denom = abs(log_g)
    if denom < 1e-9:
        # gold is HR ≈ 1; use absolute log difference vs log(1+threshold)
        diff = abs(log_a - log_g)
        ref = math.log(1.0 + accurate_threshold)
        band = (Band.ACCURATE if diff <= ref
                else Band.MINOR if diff <= math.log(1.0 + minor_threshold)
                else Band.MAJOR)
        return DiscrepancyResult(band, _POINTS[band], "log_ratio_abs", diff,
                                 f"|log({a:.4f}) - log({g:.4f})| = {diff:.4f}; gold log≈0")
    diff = abs(log_a - log_g) / denom
    band = (Band.ACCURATE if diff <= accurate_threshold
            else Band.MINOR if diff <= minor_threshold
            else Band.MAJOR)
    return DiscrepancyResult(band, _POINTS[band], "log_ratio_pct", diff,
                             f"|log({a:.4f}) - log({g:.4f})| / |log({g:.4f})| = {diff:.3%}")


_PVALUE_BUCKETS = [0.001, 0.01, 0.05, 1.01]   # last sentinel covers >= 0.05
_PVALUE_BUCKET_NAMES = ["<0.001", "<0.01", "<0.05", "≥0.05"]


def _pvalue_bucket(p: float) -> int:
    for i, edge in enumerate(_PVALUE_BUCKETS):
        if p < edge:
            return i
    return len(_PVALUE_BUCKETS) - 1


def score_pvalue_bucket(agent: dict, gold: dict) -> DiscrepancyResult:
    a = _safe_float(agent.get("value"))
    g = _safe_float(gold.get("value"))
    if a is None or g is None:
        return DiscrepancyResult(Band.MAJOR, 0, "pvalue_bucket", None,
                                 f"missing value (agent={agent.get('value')!r}, gold={gold.get('value')!r})")
    a = max(0.0, min(1.0, a))
    g = max(0.0, min(1.0, g))
    ba = _pvalue_bucket(a)
    bg = _pvalue_bucket(g)
    diff = abs(ba - bg)
    band = (Band.ACCURATE if diff == 0
            else Band.MINOR if diff == 1
            else Band.MAJOR)
    return DiscrepancyResult(
        band, _POINTS[band], "pvalue_bucket", float(diff),
        f"agent_p={a:.4g} → {_PVALUE_BUCKET_NAMES[ba]}; gold_p={g:.4g} → {_PVALUE_BUCKET_NAMES[bg]}",
    )


def score_categorical(agent: dict, gold: dict) -> DiscrepancyResult:
    a = (agent.get("value") or "").strip().lower()
    g = (gold.get("value") or "").strip().lower()
    if not a or not g:
        return DiscrepancyResult(Band.MAJOR, 0, "exact_match", None,
                                 f"missing value (agent={agent.get('value')!r}, gold={gold.get('value')!r})")
    band = Band.ACCURATE if a == g else Band.MAJOR
    return DiscrepancyResult(band, _POINTS[band], "exact_match", 0.0 if band == Band.ACCURATE else 1.0,
                             f"agent={agent.get('value')!r} {'==' if band == Band.ACCURATE else '!='} gold={gold.get('value')!r}")


def score_categorical_distribution(agent: dict, gold: dict, *, mode: str = "relative",
                                   accurate_threshold: float = _ACC_THRESH,
                                   minor_threshold: float = _MIN_THRESH) -> DiscrepancyResult:
    """Score a categorical-frequency distribution by per-category max deviation.

    The band of the worst-fitting category determines the answer's band, so an
    agent cannot hide a wrong small-category estimate behind correct big ones.
    Agent and gold must have identical category key sets; missing or extra keys
    are MAJOR. Per-category comparison uses the same `relative` (default) or
    `absolute_pp` mode as `score_proportion`.
    """
    a_dist = agent.get("proportions_by_category")
    g_dist = gold.get("proportions_by_category")
    if not isinstance(a_dist, dict) or not isinstance(g_dist, dict) or not a_dist or not g_dist:
        return DiscrepancyResult(Band.MAJOR, 0, "missing_distribution", None,
                                 f"missing proportions_by_category (agent={a_dist!r}, gold={g_dist!r})")
    if set(a_dist.keys()) != set(g_dist.keys()):
        only_a = set(a_dist) - set(g_dist)
        only_g = set(g_dist) - set(a_dist)
        return DiscrepancyResult(Band.MAJOR, 0, "category_key_mismatch", None,
                                 f"category keys differ (agent_only={sorted(only_a)}, gold_only={sorted(only_g)})")
    worst_band = Band.ACCURATE
    worst_diff = 0.0
    worst_cat = None
    for cat in g_dist:
        sub = score_proportion({"value": a_dist[cat]}, {"value": g_dist[cat]},
                               mode=mode,
                               accurate_threshold=accurate_threshold,
                               minor_threshold=minor_threshold)
        if _POINTS[sub.band] < _POINTS[worst_band]:
            worst_band = sub.band
            worst_diff = sub.metric_value if sub.metric_value is not None else worst_diff
            worst_cat = cat
        elif sub.metric_value is not None and sub.metric_value > worst_diff:
            worst_diff = sub.metric_value
            if worst_cat is None:
                worst_cat = cat
    metric = "max_per_category_absolute_pp" if mode == "absolute_pp" else "max_per_category_relative_pct"
    if worst_cat is None:
        explanation = "all categories within accurate threshold"
    else:
        explanation = (f"worst category {worst_cat!r}: agent={a_dist[worst_cat]!r} "
                       f"gold={g_dist[worst_cat]!r} ({metric}={worst_diff:.3%})")
    return DiscrepancyResult(worst_band, _POINTS[worst_band], metric, worst_diff, explanation)


def _score_relative(a, g, accurate_threshold: float, minor_threshold: float) -> DiscrepancyResult:
    af = _safe_float(a)
    gf = _safe_float(g)
    if af is None or gf is None:
        return DiscrepancyResult(Band.MAJOR, 0, "relative_pct", None,
                                 f"missing value (agent={a!r}, gold={g!r})")
    if gf == 0:
        # If gold is exactly 0, treat any nonzero answer as MAJOR; equal as ACCURATE
        if af == 0:
            return DiscrepancyResult(Band.ACCURATE, 2, "relative_pct", 0.0, "both zero")
        return DiscrepancyResult(Band.MAJOR, 0, "relative_pct", float("inf"),
                                 f"gold is 0 but agent is {af}")
    diff = abs(af - gf) / abs(gf)
    band = (Band.ACCURATE if diff <= accurate_threshold
            else Band.MINOR if diff <= minor_threshold
            else Band.MAJOR)
    return DiscrepancyResult(band, _POINTS[band], "relative_pct", diff,
                             f"|{af} - {gf}| / |{gf}| = {diff:.3%}")


SCORERS = {
    "count": score_count,
    "proportion": score_proportion,
    "median_with_ci": score_median_with_ci,
    "hazard_ratio_with_ci": score_ratio_log,
    "odds_ratio_with_ci": score_ratio_log,
    "pvalue": score_pvalue_bucket,
    "categorical": score_categorical,
    "categorical_distribution": score_categorical_distribution,
}


def score_analysis(*, agent_answer: dict, gold_answer: dict, answer_type: str,
                   options: Optional[dict] = None) -> DiscrepancyResult:
    """Dispatch to the right scorer for the given answer_type.

    Special case — when the gold answer carries `unanswerable: true`, the
    estimand is structurally unidentifiable on the supplied cohort. Agents
    that recognise this (by setting `unanswerable: true` themselves OR by
    matching the placeholder gold value) are credited as ACCURATE; agents
    that produce a confident different value are MAJOR (the analysis cannot
    be computed, so a confident answer is wrong).
    """
    options = options or {}
    if gold_answer.get("unanswerable") is True:
        agent_unans = agent_answer.get("unanswerable") is True
        agent_value = _safe_float(agent_answer.get("value"))
        gold_value = _safe_float(gold_answer.get("value"))
        if agent_unans:
            return DiscrepancyResult(Band.ACCURATE, _POINTS[Band.ACCURATE],
                                     "unanswerable_match", 0.0,
                                     "agent flagged unanswerable; gold also unanswerable")
        if agent_value is not None and gold_value is not None and abs(agent_value - gold_value) <= 1e-6:
            return DiscrepancyResult(Band.ACCURATE, _POINTS[Band.ACCURATE],
                                     "unanswerable_match", 0.0,
                                     f"agent value matched gold placeholder ({gold_value}); gold unanswerable")
        return DiscrepancyResult(Band.MAJOR, _POINTS[Band.MAJOR],
                                 "unanswerable_mismatch", None,
                                 f"gold is unanswerable (sample-size); agent returned value={agent_answer.get('value')!r}")
    fn = SCORERS.get(answer_type)
    if fn is None:
        return DiscrepancyResult(Band.MAJOR, 0, "unknown_type", None,
                                 f"unknown answer_type: {answer_type}")
    # `proportion` and `categorical_distribution` accept a `mode` kwarg
    if answer_type in ("proportion", "categorical_distribution"):
        return fn(agent_answer, gold_answer, **options)
    return fn(agent_answer, gold_answer)
