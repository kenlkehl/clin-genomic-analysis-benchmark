#!/usr/bin/env python
"""Regenerate BOTH question banks from the current review workbook.

Source of truth: the review workbook under the gold root (CLINGEN_GOLD_ROOT).
Writes two banks:
  - GOLD  (out-of-repo, gold_root/questions/<cohort>.yaml): full, with answers —
    read only by the harness at scoring/compute time.
  - PUBLIC (in-repo, questions/<cohort>.yaml): gold-free (id/category/text only) —
    served to the agent under evaluation.

Maps the review columns into the CohortQuestionFile / Question Pydantic schema and validates.
Keeping the answers out of the in-repo bank is what prevents the agent from reading gold.

Component gold comes from four optional flag columns,
``<component>_needs_clarification`` (Y/N), with free-text
``<component>_notes`` alongside. A row with all four flags filled is reviewed
under the four-component rubric; a row with none is unreviewed and gets no
``component_assessment`` (scoring refuses it). The legacy concept-menu columns
are carried into the gold bank as unscored reviewer context.
"""
from __future__ import annotations
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clin_genomic_analysis_benchmark import config  # noqa: E402
from clin_genomic_analysis_benchmark.components import (  # noqa: E402
    COMPONENT_IDS,
)
ROOT = config.REPO_ROOT
from clin_genomic_analysis_benchmark.questions.schema import (  # noqa: E402
    CohortQuestionFile, ComponentAssessment, ComponentVerdict, Question, AnalysisSpec,
    GoldAnswer, SupportingEvidence,
)
from clin_genomic_analysis_benchmark.questions import io as q_io  # noqa: E402

SRC = config.workbook_path()
MODEL = "claude-opus-4-8@vertex"
DIAG_KEYS = {"cell_sizes", "cell_counts", "note", "notes", "small_cell_flag",
             "penalizer_used", "failure_reason"}

def parse_tables(gold_script: str) -> list[str]:
    # gold_script values are like "gold_standard/<cohort>/<qid>.py", now rooted
    # under the (out-of-repo) gold root.
    p = config.gold_root() / gold_script
    if not gold_script or not p.exists():
        return ["cancer_level_dataset_index.csv"]
    t = p.read_text()
    tabs = set(re.findall(r'"([A-Za-z0-9_./-]+\.(?:csv|txt))"', t)) | \
           set(re.findall(r"'([A-Za-z0-9_./-]+\.(?:csv|txt))'", t))
    tabs = sorted({Path(x).name for x in tabs})
    return tabs or ["cancer_level_dataset_index.csv"]

def population_unit(text: str, atype: str) -> str:
    tl = (text or "").lower()
    if re.search(r"\bsamples?\b", tl):
        return "sample"
    if "per index" in tl or "per colorectal" in tl or "per prostate" in tl or "per pancreatic" in tl:
        return "cancer"
    return "patient"

def build_gold_answer(qid: str, raw: str, atype: str) -> dict:
    full = json.loads(raw)
    d = {k: v for k, v in full.items() if k not in DIAG_KEYS}
    if qid == "prostate_1.2-Qf17acd7c":   # well-posed but not estimable in this cohort
        d["unanswerable"] = True
        d["unanswerable_reason"] = str(
            full.get("failure_reason") or full.get("value") or "estimand is not identifiable")
    return d


FLAG_COLUMNS = {cid: f"{cid}_needs_clarification" for cid in COMPONENT_IDS}
NOTES_COLUMNS = {cid: f"{cid}_notes" for cid in COMPONENT_IDS}
_TRUE = {"y", "yes", "true", "1"}
_FALSE = {"n", "no", "false", "0"}


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_flag(value: object, *, qid: str, column: str) -> Optional[bool]:
    """Read one Y/N flag cell; None when blank."""
    if _blank(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if isinstance(value, (int, float)) and value in (0, 1):
        text = str(int(value))
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{qid}: {column} must be Y or N, got {value!r}")


def parse_component_assessment(row: Mapping[str, object], *, qid: str,
                               classification: str) -> Optional[ComponentAssessment]:
    """Build the component gold for one row, or None when it is unreviewed."""
    flags = {cid: parse_flag(row.get(col), qid=qid, column=col)
             for cid, col in FLAG_COLUMNS.items()}
    filled = [cid for cid, flag in flags.items() if flag is not None]
    if not filled:
        return None
    if len(filled) != len(COMPONENT_IDS):
        missing = [FLAG_COLUMNS[cid] for cid in COMPONENT_IDS if flags[cid] is None]
        raise ValueError(
            f"{qid}: component flags are partially filled; missing {', '.join(missing)}")
    assessment = ComponentAssessment(**{
        cid: ComponentVerdict(
            needs_clarification=flags[cid],
            notes=(None if _blank(row.get(NOTES_COLUMNS[cid]))
                   else str(row.get(NOTES_COLUMNS[cid])).strip()),
        )
        for cid in COMPONENT_IDS
    })
    if assessment.classification != classification:
        raise ValueError(
            f"{qid}: classification column says {classification!r} but the component "
            f"flags imply {assessment.classification!r}")
    return assessment


def parse_legacy_concepts(raw: object) -> list[str]:
    """Legacy concept prose, one concept per line."""
    return [ln.strip() for ln in str(raw or "").split("\n") if ln.strip()]


def parse_legacy_concept_ids(raw: object) -> Optional[list[str]]:
    """Legacy concept-menu IDs as written; not validated against any menu."""
    if _blank(raw):
        return None
    text = str(raw).strip()
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        ids = [str(value).strip() for value in decoded if str(value).strip()]
    else:
        ids = [value.strip() for value in re.split(r"[\n,]", text) if value.strip()]
    return ids or None


def build_question(row: Mapping[str, object]) -> Question:
    """Map one workbook row (header name -> cell value) to a gold Question."""
    qid = row.get("qid")
    cls = row.get("classification")
    common = dict(
        id=qid, category=int(row.get("category")), text=row.get("question_text"),
        classification=cls, source=(row.get("source") or "llm"), review_status="reviewed",
        component_assessment=parse_component_assessment(
            row, qid=str(qid), classification=str(cls)),
    )
    if cls == "unambiguous":
        atype = row.get("expected_answer_type")
        gold_script = row.get("gold_script") or ""
        return Question(
            **common,
            analysis_spec=AnalysisSpec(
                population_unit=population_unit(row.get("question_text"), atype),
                tables=parse_tables(gold_script),
                statistic=(row.get("analysis_plan_summary") or f"{atype} per analysis plan").strip(),
                expected_answer_type=atype,
            ),
            gold_answer=GoldAnswer(**build_gold_answer(qid, row.get("gold_answer"), atype)),
            gold_supporting_evidence=SupportingEvidence(gold_script=gold_script or None),
        )
    return Question(
        **common,
        disambiguation_concept_ids=parse_legacy_concept_ids(
            row.get("disambiguation_concept_ids")),
        disambiguation_concepts=parse_legacy_concepts(row.get("disambiguation_concepts")),
    )


def read_rows(ws) -> list[dict[str, object]]:
    """Read the questions sheet as dicts keyed by header; absent columns read as None."""
    hdr = [c.value for c in ws[1]]
    rows = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        if all(_blank(v) for v in values):
            continue
        rows.append({name: value for name, value in zip(hdr, values) if name})
    return rows


def main() -> int:
    wb = openpyxl.load_workbook(SRC)
    ws = wb["questions"]

    by_cohort: dict[str, list[Question]] = {}
    n_un = n_amb = n_reviewed = 0
    for row in read_rows(ws):
        q = build_question(row)
        if q.classification == "unambiguous":
            n_un += 1
        else:
            n_amb += 1
        if q.component_assessment is not None:
            n_reviewed += 1
        by_cohort.setdefault(row["cohort"], []).append(q)

    now = datetime.now(timezone.utc)
    total = 0
    for cohort, qs in by_cohort.items():
        cqf = CohortQuestionFile(cohort=cohort, generated_at=now, model=MODEL, questions=qs)
        gold_path = q_io.save_gold(cqf)                       # full, out-of-repo
        pub_path = q_io.save_public(q_io.to_public(cqf))      # gold-free, in-repo
        total += len(qs)
        print(f"  {cohort}: {len(qs)} questions -> gold {gold_path}  |  public {pub_path}")
    print(f"TOTAL: {total} questions ({n_un} unambiguous / {n_amb} ambiguous) across {len(by_cohort)} cohorts")
    print(f"COMPONENT GOLD: {n_reviewed} reviewed / {total - n_reviewed} unreviewed "
          "(unreviewed questions cannot be scored)")
    print(f"GOLD ROOT: {config.gold_root()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
