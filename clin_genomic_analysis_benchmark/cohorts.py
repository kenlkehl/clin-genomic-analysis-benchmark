"""Cohort registry and per-cohort file enumeration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import data_root


@dataclass(frozen=True)
class Cohort:
    name: str               # folder name under bpc_from_synapse
    label: str              # human-readable label, e.g. "Bladder (BPC v1.2)"
    cancer_type: str        # canonical disease, e.g. "bladder"
    release: str            # e.g. "v1.2-consortium" or "v2.0-public"

    @property
    def path(self) -> Path:
        return data_root() / self.name


COHORTS: list[Cohort] = [
    Cohort("bladder_1.2",       "Bladder (BPC v1.2 consortium)",  "bladder",  "v1.2-consortium"),
    Cohort("breast_1.2",        "Breast (BPC v1.2 consortium)",   "breast",   "v1.2-consortium"),
    Cohort("crc_2.0_public",    "CRC (BPC v2.0 public)",          "crc",      "v2.0-public"),
    Cohort("nsclc_2.0_public",  "NSCLC (BPC v2.0 public)",        "nsclc",    "v2.0-public"),
    Cohort("panc_1.2",          "Pancreatic (BPC v1.2 consortium)","panc",    "v1.2-consortium"),
    Cohort("prostate_1.2",      "Prostate (BPC v1.2 consortium)", "prostate", "v1.2-consortium"),
]

COHORTS_BY_NAME: dict[str, Cohort] = {c.name: c for c in COHORTS}


def get_cohort(name: str) -> Cohort:
    if name not in COHORTS_BY_NAME:
        raise KeyError(f"Unknown cohort: {name}. Known: {list(COHORTS_BY_NAME)}")
    return COHORTS_BY_NAME[name]


def resolve_cohorts(name_or_all: str) -> list[Cohort]:
    """Resolve a CLI cohort spec ('all' or a name) to a list of Cohort objects."""
    if name_or_all == "all":
        return list(COHORTS)
    return [get_cohort(name_or_all)]


def list_files(cohort: Cohort) -> dict[str, Path]:
    """Enumerate all files in a cohort folder (non-recursive). Returns {filename: abspath}."""
    if not cohort.path.exists():
        raise FileNotFoundError(f"Cohort directory missing: {cohort.path}")
    return {p.name: p for p in cohort.path.iterdir() if p.is_file()}


def find_data_dictionary(cohort: Cohort) -> Path:
    """Return the most parser-friendly data dictionary file for the cohort.

    Prefers `simple_variable_synopsis.xlsx` (clean per-variable rows) over the
    full `BPC_*_variable_synopsis.xlsx` (multi-sheet workbook).
    """
    files = list_files(cohort)
    if "simple_variable_synopsis.xlsx" in files:
        return files["simple_variable_synopsis.xlsx"]
    for name, p in files.items():
        if name.startswith("BPC_") and name.endswith("_variable_synopsis.xlsx"):
            return p
    raise FileNotFoundError(f"No data dictionary found in {cohort.path}")


def categorize_files(cohort: Cohort) -> dict[str, list[Path]]:
    """Group cohort files by type (clinical, genomic, timeline, derived, dictionary, other)."""
    files = list_files(cohort)
    out: dict[str, list[Path]] = {
        "dictionary": [],
        "clinical": [],
        "genomic": [],
        "timeline": [],
        "derived": [],
        "case_lists": [],
        "meta": [],
        "other": [],
    }
    for name, p in sorted(files.items()):
        if name.endswith("_variable_synopsis.xlsx"):
            out["dictionary"].append(p)
        elif name.startswith("data_clinical"):
            out["clinical"].append(p)
        elif name.startswith("data_timeline"):
            out["timeline"].append(p)
        elif name in {"data_mutations_extended.txt", "data_CNA.txt", "data_cna_hg19.seg",
                      "data_fusions.txt", "data_sv.txt", "data_gene_matrix.txt",
                      "genomic_information.txt"} or name.startswith("data_gene_panel_"):
            out["genomic"].append(p)
        elif name.endswith(".csv") and ("_dataset" in name or name.startswith("ca_radtx")):
            out["derived"].append(p)
        elif name.startswith("meta_") or name.endswith("SYNAPSE_METADATA_MANIFEST.tsv"):
            out["meta"].append(p)
        else:
            out["other"].append(p)
    case_lists_dir = cohort.path / "case_lists"
    if case_lists_dir.exists():
        out["case_lists"] = sorted(case_lists_dir.iterdir())
    return out
