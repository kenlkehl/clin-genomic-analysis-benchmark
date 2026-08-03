"""Build a compact, cached "cohort context" for prompt inclusion.

The cohort context summarises:
  - which files exist (grouped by category),
  - each tabular file's column list with dtype hints + low-cardinality value samples,
  - row counts.

This is computed once per cohort and cached on disk so subsequent question-gen
or gold-codegen calls reuse the same context (good for prompt caching).
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .cohorts import Cohort, categorize_files
from .config import CACHE_DIR

# Tabular-file extensions we'll inspect
_TABULAR_SUFFIXES = (".txt", ".tsv", ".csv", ".seg", ".maf")
# Files we never bother sampling (purely metadata)
_SKIP_PREFIXES = ("meta_", "SYNAPSE_METADATA")


@dataclass
class ColumnSummary:
    name: str
    dtype: str                          # numeric|integer|categorical|text|date|unknown
    distinct_observed: int
    sample_values: list[str]            # up to 5 distinct values (for low-cardinality cols)
    null_fraction: float = 0.0


@dataclass
class TableSummary:
    filename: str
    delimiter: str                      # "\t" or ","
    n_rows_sampled: int                 # rows actually inspected (capped)
    n_columns: int
    columns: list[ColumnSummary] = field(default_factory=list)


@dataclass
class CohortContext:
    cohort: str
    files_by_category: dict[str, list[str]]
    tables: dict[str, TableSummary]     # filename -> summary

    def to_dict(self) -> dict:
        return {
            "cohort": self.cohort,
            "files_by_category": self.files_by_category,
            "tables": {k: _table_to_dict(v) for k, v in self.tables.items()},
        }


def _table_to_dict(t: TableSummary) -> dict:
    return {
        "filename": t.filename,
        "delimiter": "tab" if t.delimiter == "\t" else "comma",
        "n_rows_sampled": t.n_rows_sampled,
        "n_columns": t.n_columns,
        "columns": [asdict(c) for c in t.columns],
    }


def _detect_delimiter(path: Path) -> str:
    if path.suffix.lower() in {".csv"}:
        return ","
    return "\t"


def _classify_dtype(values: list[str]) -> str:
    if not values:
        return "unknown"
    n_int = n_num = n_date = 0
    for v in values:
        v = v.strip()
        if not v:
            continue
        try:
            int(v)
            n_int += 1
            continue
        except ValueError:
            pass
        try:
            float(v)
            n_num += 1
            continue
        except ValueError:
            pass
        if len(v) >= 8 and (v.count("-") == 2 or v.count("/") == 2) and any(c.isdigit() for c in v):
            n_date += 1
    n_total = len([v for v in values if v.strip()])
    if n_total == 0:
        return "unknown"
    if n_int / n_total > 0.9:
        return "integer"
    if (n_int + n_num) / n_total > 0.9:
        return "numeric"
    if n_date / n_total > 0.7:
        return "date"
    n_distinct = len({v.strip() for v in values if v.strip()})
    if n_distinct <= max(20, int(0.05 * n_total)):
        return "categorical"
    return "text"


def _summarize_table(path: Path, max_rows: int = 2000, max_sample_values: int = 5) -> Optional[TableSummary]:
    """Stream up to `max_rows` rows from a tabular file and summarise its columns.

    cBioPortal clinical files (`data_clinical_*.txt`) have FIVE header lines:
      #<display labels>
      #<descriptions>
      #<types: STRING/NUMBER/...>
      #<priorities>
      <MACHINE_COLUMN_IDS>          ← we use this as the header
    For all other tabular files, the first non-`#` line is the header.
    """
    if path.name.startswith(_SKIP_PREFIXES) or path.suffix.lower() not in _TABULAR_SUFFIXES:
        return None
    delimiter = _detect_delimiter(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            # Skip ALL leading `#`-prefixed rows; the first non-`#` line is the header.
            header_fields: Optional[list[str]] = None
            cleaned_lines: list[str] = []
            for raw in f:
                if not raw.strip():
                    continue
                if raw.startswith("#"):
                    continue
                cleaned_lines.append(raw)
                if len(cleaned_lines) > max_rows + 5:
                    break

            reader = csv.reader(cleaned_lines, delimiter=delimiter)
            try:
                header_fields = next(reader)
            except StopIteration:
                return None

            data_rows: list[list[str]] = []
            for fields in reader:
                if len(fields) < len(header_fields):
                    fields = fields + [""] * (len(header_fields) - len(fields))
                data_rows.append(fields[: len(header_fields)])
                if len(data_rows) >= max_rows:
                    break
    except Exception:
        return None
    if not header_fields:
        return None

    columns: list[ColumnSummary] = []
    for ci, cname in enumerate(header_fields):
        col_values = [r[ci] for r in data_rows if ci < len(r)]
        nonnull = [v for v in col_values if v not in ("", "NA", "Unknown", "Not Available")]
        distinct = sorted({v for v in nonnull})
        sample = distinct[:max_sample_values]
        dtype = _classify_dtype(nonnull)
        null_frac = 1.0 - (len(nonnull) / len(col_values)) if col_values else 0.0
        columns.append(
            ColumnSummary(
                name=cname.strip(),
                dtype=dtype,
                distinct_observed=len(distinct),
                sample_values=sample,
                null_fraction=round(null_frac, 3),
            )
        )

    return TableSummary(
        filename=path.name,
        delimiter=delimiter,
        n_rows_sampled=len(data_rows),
        n_columns=len(header_fields),
        columns=columns,
    )


def build(cohort: Cohort, use_cache: bool = True, max_rows_per_file: int = 2000) -> CohortContext:
    """Build (or load cached) cohort context."""
    cache_path = CACHE_DIR / "context" / f"{cohort.name}.json"
    if use_cache and cache_path.exists():
        data = json.loads(cache_path.read_text())
        return _context_from_dict(data)

    grouped = categorize_files(cohort)
    files_by_category = {k: [p.name for p in v] for k, v in grouped.items() if v}

    tables: dict[str, TableSummary] = {}
    for cat in ("clinical", "genomic", "timeline", "derived"):
        for p in grouped.get(cat, []):
            ts = _summarize_table(p, max_rows=max_rows_per_file)
            if ts is not None:
                tables[p.name] = ts

    ctx = CohortContext(cohort=cohort.name, files_by_category=files_by_category, tables=tables)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(ctx.to_dict(), indent=2))
    return ctx


def _context_from_dict(data: dict) -> CohortContext:
    tables = {}
    for fn, td in data.get("tables", {}).items():
        cols = [ColumnSummary(**c) for c in td.get("columns", [])]
        tables[fn] = TableSummary(
            filename=td["filename"],
            delimiter="\t" if td.get("delimiter") == "tab" else ",",
            n_rows_sampled=td.get("n_rows_sampled", 0),
            n_columns=td.get("n_columns", 0),
            columns=cols,
        )
    return CohortContext(
        cohort=data["cohort"],
        files_by_category=data.get("files_by_category", {}),
        tables=tables,
    )


def to_compact_markdown(ctx: CohortContext, max_cols_listed: int = 0) -> str:
    """Compact Markdown rendering of the cohort context for LLM prompts.

    `max_cols_listed=0` means list all columns; set to a positive number to truncate
    long column lists.
    """
    lines: list[str] = [f"# Cohort: {ctx.cohort}"]
    lines.append("\n## Files by category")
    for cat, names in ctx.files_by_category.items():
        if not names:
            continue
        lines.append(f"- **{cat}**: {', '.join(names)}")
    lines.append("\n## Tabular files (column summaries)")
    for fn in sorted(ctx.tables):
        t = ctx.tables[fn]
        lines.append(f"\n### {fn}  ({t.n_columns} cols, sampled {t.n_rows_sampled} rows, delim={'tab' if t.delimiter == chr(9) else 'comma'})")
        cols = t.columns if max_cols_listed <= 0 else t.columns[:max_cols_listed]
        for c in cols:
            sample = ", ".join(c.sample_values)
            sample = f" ; samples: [{sample}]" if sample else ""
            lines.append(f"- `{c.name}` [{c.dtype}; distinct={c.distinct_observed}; null={c.null_fraction}]{sample}")
        if max_cols_listed > 0 and len(t.columns) > max_cols_listed:
            lines.append(f"- … {len(t.columns) - max_cols_listed} more columns truncated")
    return "\n".join(lines)
