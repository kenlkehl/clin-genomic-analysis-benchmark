"""The four question components judged in the assess stage.

The agent decides, for each component, whether the question needs
clarification before one analysis pipeline follows from it. A question is
ambiguous when any component needs clarification. The rules for each
component live in AGENT_INSTRUCTIONS.md; this module only fixes the IDs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    description: str


COMPONENTS: tuple[Component, ...] = (
    Component(
        "denominator",
        "Cohort denominator",
        "Which patients or records are analysed: cancer type and histology, disease "
        "burden, line of therapy, qualifying treatment, genomic-testing timing (for "
        "questions that touch genomics), and the unit of analysis.",
    ),
    Component(
        "outcome",
        "Outcome",
        "What is measured for each analysed unit, and how missing values are handled; "
        "for time-to-event outcomes, the index date, how events are ascertained, and "
        "censoring or competing events.",
    ),
    Component(
        "covariates",
        "Independent variables and covariates",
        "Exposures, comparison groups, stratifiers, and adjustment covariates, each "
        "defined so that every analysed record falls in exactly one category or takes "
        "exactly one value.",
    ),
    Component(
        "analysis_plan",
        "Statistical analysis plan",
        "How the estimate is computed: the statistical estimand, the model "
        "architecture, the variables in any multivariable model, left truncation, and "
        "exposures ascertained after the index date.",
    ),
)

COMPONENT_IDS: tuple[str, ...] = tuple(component.id for component in COMPONENTS)
COMPONENTS_BY_ID: dict[str, Component] = {component.id: component for component in COMPONENTS}

Classification = Literal["ambiguous", "unambiguous"]


def components_payload() -> list[dict[str, str]]:
    """Return the component list in canonical order for question.json."""
    return [
        {"id": component.id, "label": component.label, "description": component.description}
        for component in COMPONENTS
    ]


def classification_from_flags(flags: Mapping[str, bool]) -> Classification:
    """A question is ambiguous when any component needs clarification."""
    missing = [cid for cid in COMPONENT_IDS if cid not in flags]
    if missing:
        raise ValueError(f"missing component verdicts: {', '.join(missing)}")
    return "ambiguous" if any(flags[cid] for cid in COMPONENT_IDS) else "unambiguous"


def flags_from_assess_result(result: Mapping[str, object]) -> dict[str, bool]:
    """Read ``needs_clarification`` for each component from an assess result.

    The result must already have passed contract validation.
    """
    flags: dict[str, bool] = {}
    for cid in COMPONENT_IDS:
        verdict = result[cid]
        if not isinstance(verdict, Mapping) or not isinstance(
                verdict.get("needs_clarification"), bool):
            raise ValueError(f"{cid}: needs_clarification must be a boolean")
        flags[cid] = verdict["needs_clarification"]
    return flags
