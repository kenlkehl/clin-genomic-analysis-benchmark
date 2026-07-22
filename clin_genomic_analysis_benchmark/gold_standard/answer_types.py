"""Answer-type registry: required fields + numeric-range checks for gold answers.

Each answer-type defines:
  - required_fields: keys that MUST be present on the answer dict
  - optional_fields: keys that MAY be present
  - validate(answer) → list of error strings (empty if valid)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_range(field: str, value: Any, lo: float | None = None, hi: float | None = None) -> list[str]:
    errs: list[str] = []
    if not _is_number(value):
        return [f"{field} must be a number, got {type(value).__name__}"]
    if lo is not None and value < lo:
        errs.append(f"{field}={value} below minimum {lo}")
    if hi is not None and value > hi:
        errs.append(f"{field}={value} above maximum {hi}")
    return errs


@dataclass(frozen=True)
class AnswerSpec:
    name: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    validator: Callable[[dict], list[str]]


def _validate_count(a: dict) -> list[str]:
    errs: list[str] = []
    errs += _check_range("value", a.get("value"), lo=0)
    if "n" in a:
        errs += _check_range("n", a["n"], lo=0)
    return errs


def _validate_proportion(a: dict) -> list[str]:
    errs: list[str] = []
    errs += _check_range("value", a.get("value"), lo=0.0, hi=1.0)
    if "numerator" in a:
        errs += _check_range("numerator", a["numerator"], lo=0)
    if "denominator" in a:
        errs += _check_range("denominator", a["denominator"], lo=1)
        num = a.get("numerator")
        den = a.get("denominator")
        if _is_number(num) and _is_number(den) and num > den:
            errs.append(f"numerator ({num}) > denominator ({den})")
    if "ci_low" in a and a["ci_low"] is not None:
        errs += _check_range("ci_low", a["ci_low"], lo=0.0, hi=1.0)
    if "ci_high" in a and a["ci_high"] is not None:
        errs += _check_range("ci_high", a["ci_high"], lo=0.0, hi=1.0)
    return errs


def _validate_median_with_ci(a: dict) -> list[str]:
    errs: list[str] = []
    errs += _check_range("value", a.get("value"), lo=0)
    for f in ("ci_low", "ci_high"):
        if f in a and a[f] is not None:
            errs += _check_range(f, a[f], lo=0)
    if "n_total" in a:
        errs += _check_range("n_total", a["n_total"], lo=1)
    if "n_events" in a:
        errs += _check_range("n_events", a["n_events"], lo=0)
    return errs


def _validate_ratio_with_ci(a: dict) -> list[str]:
    errs: list[str] = []
    errs += _check_range("value", a.get("value"), lo=0)
    for f in ("ci_low", "ci_high"):
        if f in a and a[f] is not None:
            errs += _check_range(f, a[f], lo=0)
    if "p_value" in a and a["p_value"] is not None:
        errs += _check_range("p_value", a["p_value"], lo=0.0, hi=1.0)
    if "n_total" in a:
        errs += _check_range("n_total", a["n_total"], lo=1)
    return errs


def _validate_pvalue(a: dict) -> list[str]:
    errs: list[str] = []
    errs += _check_range("value", a.get("value"), lo=0.0, hi=1.0)
    if "n_total" in a:
        errs += _check_range("n_total", a["n_total"], lo=1)
    if not a.get("test_name"):
        errs.append("test_name is required for pvalue answers")
    return errs


def _validate_categorical(a: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(a.get("value"), str) or not a["value"].strip():
        errs.append("value must be a non-empty string for categorical answers")
    if "n_total" in a:
        errs += _check_range("n_total", a["n_total"], lo=1)
    return errs


def _validate_categorical_distribution(a: dict) -> list[str]:
    errs: list[str] = []
    props = a.get("proportions_by_category")
    if not isinstance(props, dict) or not props:
        errs.append("proportions_by_category must be a non-empty dict of {category: proportion}")
        return errs
    for k, v in props.items():
        if not isinstance(k, str) or not k.strip():
            errs.append(f"proportions_by_category has non-string or empty key: {k!r}")
        errs += _check_range(f"proportions_by_category[{k!r}]", v, lo=0.0, hi=1.0)
    total = sum(v for v in props.values() if _is_number(v))
    if abs(total - 1.0) > 1e-3:
        errs.append(f"proportions_by_category must sum to 1.0 (got {total:.6f})")
    counts = a.get("counts_by_category")
    if counts is not None:
        if not isinstance(counts, dict):
            errs.append("counts_by_category must be a dict when provided")
        else:
            if set(counts.keys()) != set(props.keys()):
                errs.append("counts_by_category keys must match proportions_by_category keys")
            for k, v in counts.items():
                errs += _check_range(f"counts_by_category[{k!r}]", v, lo=0)
    denom = a.get("denominator")
    if denom is not None:
        errs += _check_range("denominator", denom, lo=1)
        if isinstance(counts, dict):
            csum = sum(v for v in counts.values() if _is_number(v))
            if csum != denom:
                errs.append(f"counts_by_category sums to {csum}, but denominator={denom}")
    return errs


ANSWER_TYPES: dict[str, AnswerSpec] = {
    "count": AnswerSpec(
        "count", ("value",), ("n",), _validate_count,
    ),
    "proportion": AnswerSpec(
        "proportion", ("value", "numerator", "denominator"),
        ("ci_low", "ci_high"), _validate_proportion,
    ),
    "median_with_ci": AnswerSpec(
        "median_with_ci", ("value", "ci_low", "ci_high"),
        ("n_total", "n_events"), _validate_median_with_ci,
    ),
    "hazard_ratio_with_ci": AnswerSpec(
        "hazard_ratio_with_ci", ("value", "ci_low", "ci_high", "p_value"),
        ("n_total", "n_events"), _validate_ratio_with_ci,
    ),
    "odds_ratio_with_ci": AnswerSpec(
        "odds_ratio_with_ci", ("value", "ci_low", "ci_high", "p_value"),
        ("n_total",), _validate_ratio_with_ci,
    ),
    "pvalue": AnswerSpec(
        "pvalue", ("value", "test_name"), ("n_total",), _validate_pvalue,
    ),
    "categorical": AnswerSpec(
        "categorical", ("value",), ("n_total",), _validate_categorical,
    ),
    "categorical_distribution": AnswerSpec(
        "categorical_distribution",
        ("proportions_by_category",),
        ("counts_by_category", "denominator"),
        _validate_categorical_distribution,
    ),
}


def validate_answer(answer_type: str, answer: dict) -> list[str]:
    """Return a list of validation error strings (empty if valid).

    When `answer["unanswerable"]` is True, the answer represents a structurally
    unidentifiable estimand (e.g., a Cox interaction model whose 2x2 design has
    empty cells on the supplied cohort). Such answers must include
    `unanswerable_reason` (free-text explanation) and `value` (a placeholder,
    typically the null-effect value: 1.0 for ratios). Other typically-required
    fields like ci_low/ci_high/p_value may be omitted because they cannot be
    estimated.
    """
    if answer_type not in ANSWER_TYPES:
        return [f"unknown answer_type: {answer_type}"]
    spec = ANSWER_TYPES[answer_type]
    errs: list[str] = []
    if not isinstance(answer, dict):
        return [f"answer must be a dict, got {type(answer).__name__}"]
    if answer.get("unanswerable") is True:
        if "value" not in answer or answer["value"] is None:
            errs.append("missing required field: value (supply a placeholder even when unanswerable)")
        if not isinstance(answer.get("unanswerable_reason"), str) or not answer["unanswerable_reason"].strip():
            errs.append("unanswerable=True requires a non-empty unanswerable_reason string")
        return errs
    for f in spec.required_fields:
        if f not in answer or answer[f] is None:
            errs.append(f"missing required field: {f}")
    errs.extend(spec.validator(answer))
    return errs
