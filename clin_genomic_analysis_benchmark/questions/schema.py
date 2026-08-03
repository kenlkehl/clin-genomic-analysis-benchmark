"""Pydantic models for the per-cohort question YAML."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..concepts import validate_concept_ids

Classification = Literal["ambiguous", "unambiguous"]
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


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: int = Field(ge=1, le=8)
    text: str
    classification: Classification
    rationale: Optional[str] = None

    # If unambiguous:
    analysis_spec: Optional[AnalysisSpec] = None
    gold_answer: Optional[GoldAnswer] = None
    gold_supporting_evidence: Optional[SupportingEvidence] = None

    # If ambiguous:
    disambiguation_concept_ids: Optional[list[str]] = None
    # Retained in the out-of-repo gold bank as human-readable audit context.
    # Scoring uses only ``disambiguation_concept_ids``.
    disambiguation_concepts: Optional[list[str]] = None

    # Provenance:
    source: Optional[str] = None              # "llm" | "human"
    review_status: Literal["draft", "reviewed", "rejected"] = "draft"
    review_notes: Optional[str] = None

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()

    @field_validator("disambiguation_concept_ids")
    @classmethod
    def _valid_concept_ids(cls, values: Optional[list[str]]) -> Optional[list[str]]:
        if values is None:
            return values
        errors = validate_concept_ids(values)
        if errors:
            raise ValueError("; ".join(errors))
        return values


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
