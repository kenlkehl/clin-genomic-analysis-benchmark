"""Row parsing for scripts/sync_yaml_from_review.py (never run end to end here)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import openpyxl
import pytest

from clin_genomic_analysis_benchmark.components import COMPONENT_IDS

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_yaml_from_review.py"
_spec = importlib.util.spec_from_file_location("sync_yaml_from_review", _SCRIPT)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

_BASE_HEADER = [
    "qid", "cohort", "category", "classification", "question_text",
    "disambiguation_concepts", "disambiguation_concept_ids", "analysis_plan_summary",
    "expected_answer_type", "gold_answer", "gold_script", "source",
]
_COMPONENT_HEADER = [
    col
    for cid in COMPONENT_IDS
    for col in (f"{cid}_needs_clarification", f"{cid}_notes")
]


def _sheet(rows: list[dict], *, with_components: bool = True):
    wb = openpyxl.Workbook()
    ws = wb.active
    header = _BASE_HEADER + (_COMPONENT_HEADER if with_components else [])
    ws.append(header)
    for row in rows:
        ws.append([row.get(name) for name in header])
    ws.append([None] * len(header))  # trailing blank row is ignored
    return ws


def _unambiguous_row(**overrides) -> dict:
    row = {
        "qid": "crc_2.0_public-Q00000001", "cohort": "crc_2.0_public", "category": 1,
        "classification": "unambiguous", "question_text": "How many patients?",
        "analysis_plan_summary": "count patients", "expected_answer_type": "count",
        "gold_answer": json.dumps({"value": 10, "n": 10}), "gold_script": "",
        "source": "human",
        "denominator_needs_clarification": "N", "outcome_needs_clarification": "no",
        "covariates_needs_clarification": False, "analysis_plan_needs_clarification": 0,
        "denominator_notes": "all patients in the cohort",
    }
    row.update(overrides)
    return row


def _ambiguous_row(**overrides) -> dict:
    row = {
        "qid": "crc_2.0_public-Q00000002", "cohort": "crc_2.0_public", "category": 5,
        "classification": "ambiguous", "question_text": "What is median PFS?",
        "disambiguation_concepts": "PFS variant\nindex date\n",
        "disambiguation_concept_ids": '["OUTCOME_METRIC", "TIME_ORIGIN"]',
        "source": "llm",
        "denominator_needs_clarification": "n", "outcome_needs_clarification": "Y",
        "covariates_needs_clarification": "N", "analysis_plan_needs_clarification": "yes",
        "outcome_notes": "PFS ascertainment and index date unstated",
    }
    row.update(overrides)
    return row


def test_reviewed_rows_build_component_gold() -> None:
    rows = sync.read_rows(_sheet([_unambiguous_row(), _ambiguous_row()]))
    assert len(rows) == 2

    clear = sync.build_question(rows[0])
    assert clear.component_assessment is not None
    assert clear.component_assessment.classification == "unambiguous"
    assert clear.component_assessment.denominator.notes == "all patients in the cohort"
    assert clear.component_assessment.outcome.notes is None
    assert clear.gold_answer.value == 10

    unclear = sync.build_question(rows[1])
    assert unclear.component_assessment.flags() == {
        "denominator": False, "outcome": True, "covariates": False, "analysis_plan": True,
    }
    assert unclear.disambiguation_concepts == ["PFS variant", "index date"]
    assert unclear.disambiguation_concept_ids == ["OUTCOME_METRIC", "TIME_ORIGIN"]


def test_unreviewed_rows_have_no_component_gold() -> None:
    blank = {f"{cid}_needs_clarification": None for cid in COMPONENT_IDS}
    rows = sync.read_rows(_sheet([_ambiguous_row(**blank)]))
    question = sync.build_question(rows[0])
    assert question.component_assessment is None
    assert question.classification == "ambiguous"

    rows = sync.read_rows(_sheet([_unambiguous_row()], with_components=False))
    assert sync.build_question(rows[0]).component_assessment is None


def test_partially_filled_flags_are_rejected() -> None:
    rows = sync.read_rows(_sheet([_ambiguous_row(covariates_needs_clarification="")]))
    with pytest.raises(ValueError, match="partially filled.*covariates_needs_clarification"):
        sync.build_question(rows[0])


def test_flags_must_agree_with_classification_column() -> None:
    rows = sync.read_rows(_sheet([_unambiguous_row(outcome_needs_clarification="Y")]))
    with pytest.raises(ValueError, match="crc_2.0_public-Q00000001.*'unambiguous'.*'ambiguous'"):
        sync.build_question(rows[0])


def test_flag_values_are_case_insensitive_and_strict() -> None:
    for value in ("Y", "yes", "TRUE", "1", 1, True):
        assert sync.parse_flag(value, qid="q", column="c") is True
    for value in ("n", "No", "false", "0", 0, False):
        assert sync.parse_flag(value, qid="q", column="c") is False
    assert sync.parse_flag("  ", qid="q", column="c") is None
    with pytest.raises(ValueError, match="q: c must be Y or N"):
        sync.parse_flag("maybe", qid="q", column="c")


def test_legacy_concept_ids_accept_lists_and_delimited_text() -> None:
    assert sync.parse_legacy_concept_ids("A, B\nC") == ["A", "B", "C"]
    assert sync.parse_legacy_concept_ids('["RETIRED_ID"]') == ["RETIRED_ID"]
    assert sync.parse_legacy_concept_ids(None) is None
