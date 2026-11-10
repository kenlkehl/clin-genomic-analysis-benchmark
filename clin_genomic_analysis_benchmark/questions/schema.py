"""Pydantic models for the per-cohort question YAML."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..components import COMPONENT_IDS, Classification, classification_from_flags
AnswerType = Literal[
    "count",
    "proportion",
    "median_with_ci",
    "hazard_ratio_with_ci",
    "odds_ratio_with_ci",
    "pvalue",
    "categorical",
    "categorical_distribution",
]
PopulationUnit = Literal["patient", "sample", "regimen", "cancer", "imaging", "pathology"]


class AnalysisSpec(BaseModel):
    """Machine-actionable analysis spec for an unambiguous question."""
    model_config = ConfigDict(extra="forbid")

    population_unit: PopulationUnit
    tables: list[str] = Field(min_length=1)
    filters: list[str] = Field(default_factory=list)
    statistic: str
    expected_answer_type: AnswerType
    notes: Optional[str] = None  # any additional spec, e.g. "use Cox PH with interaction"


class GoldAnswer(BaseModel):
    """Computed gold-standard answer. Fields beyond `value` depend on answer_type."""
    model_config = ConfigDict(extra="allow")

    value: float | int | str | None = None
    # Optional fields by answer type:
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    p_value: Optional[float] = None
    n_total: Optional[int] = None
    n_events: Optional[int] = None
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    test_name: Optional[str] = None


class SupportingEvidence(BaseModel):
    """How the gold answer was obtained — for human review and reproducibility."""
    model_config = ConfigDict(extra="allow")

    rows_used: Optional[int] = None
    gold_script: Optional[str] = None
    table_filters_applied: Optional[list[str]] = None


class ComponentVerdict(BaseModel):
    """Gold verdict for one question component."""
    model_config = ConfigDict(extra="forbid")

    needs_clarification: bool
    # What is unclear, or how the question specifies the component. Audit
    # context for reviewers; never scored.
    notes: Optional[str] = None


class ComponentAssessment(BaseModel):
    """Gold verdicts for the four components judged in the assess task."""
    model_config = ConfigDict(extra="forbid")

    denominator: ComponentVerdict
    outcome: ComponentVerdict
    covariates: ComponentVerdict
    analysis_plan: ComponentVerdict

    def flags(self) -> dict[str, bool]:
        return {cid: getattr(self, cid).needs_clarification for cid in COMPONENT_IDS}

    @property
    def classification(self) -> Classification:
        return classification_from_flags(self.flags())


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: int = Field(ge=1, le=8)
    text: str
    classification: Classification
    rationale: Optional[str] = None

    # Gold for the assess task. None until the question has been reviewed
    # under the four-component rubric; scoring refuses unreviewed questions.
    component_assessment: Optional[ComponentAssessment] = None

    # If unambiguous:
    analysis_spec: Optional[AnalysisSpec] = None
    gold_answer: Optional[GoldAnswer] = None
    gold_supporting_evidence: Optional[SupportingEvidence] = None

    # Legacy (pre-2026-10-25) concept-menu labels. Kept only as reviewer
    # context until the component review relabels the bank; never scored or
    # validated against a menu.
    disambiguation_concept_ids: Optional[list[str]] = None
    disambiguation_concepts: Optional[list[str]] = None

    # Provenance:
    source: Optional[str] = None              # "llm" | "human"
    review_status: Literal["draft", "reviewed", "rejected"] = "draft"
    review_notes: Optional[str] = None

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _classification_matches_components(self) -> "Question":
        if (self.component_assessment is not None
                and self.component_assessment.classification != self.classification):
            raise ValueError(
                f"{self.id}: classification {self.classification!r} disagrees with the "
                f"component verdicts ({self.component_assessment.classification!r})"
            )
        return self


class CohortQuestionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cohort: str
    generated_at: datetime
    model: str                               # e.g. "claude-opus-4-7@vertex"
    schema_version: str = "2"
    questions: list[Question] = Field(default_factory=list)

    def by_category(self, category: int) -> list[Question]:
        return [q for q in self.questions if q.category == category]

    def by_id(self, qid: str) -> Optional[Question]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None


# ---------------------------------------------------------------------------
# Public, gold-free bank served to the agent under evaluation. It has NO gold
# fields (classification, gold_answer, disambiguation_concepts, analysis_spec,
# ...), so it is impossible to leak the answer through this model by type.
# ---------------------------------------------------------------------------


class PublicQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: int = Field(ge=1, le=8)
    text: str

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()


class PublicCohortQuestionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cohort: str
    generated_at: datetime
    model: str
    schema_version: str = "2"
    questions: list[PublicQuestion] = Field(default_factory=list)

    def by_id(self, qid: str) -> Optional[PublicQuestion]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None
