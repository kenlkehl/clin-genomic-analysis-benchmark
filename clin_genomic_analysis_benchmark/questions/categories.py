"""The 8 question categories + per-category guidance for the LLM generator.

Each category describes:
  - which BPC tables are most relevant
  - which outcome variables / biomarker concepts to think about
  - typical answer types when unambiguous
  - guidance on common ambiguity sources
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategorySpec:
    id: int
    name: str
    description: str
    relevant_tables: list[str]
    typical_answer_types: list[str]
    common_ambiguity_sources: list[str]
    guidance: str


# Outcome variables that are stable across all BPC cohorts. The LLM should treat
# selecting among these as a key disambiguation step for outcome questions.
DIAGNOSIS_BASED_OUTCOMES = [
    "os_dx (overall survival from diagnosis, in cancer_level_dataset_index.csv → tt_os_dx_mos / os_dx_status)",
    "pfs_i_adv (PFS until investigator-determined progression, from advanced/metastatic diagnosis)",
    "pfs_m_adv (PFS until progression OR death, from advanced/metastatic diagnosis)",
]
REGIMEN_BASED_OUTCOMES = [
    "os_d (overall survival from regimen start, in regimen_cancer_level_dataset.csv → tt_os_d1_mos / os_d_status)",
    "pfs_i_g (PFS investigator-determined, from regimen start)",
    "pfs_m_g (PFS until progression OR death, from regimen start)",
]


CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        id=1,
        name="Demographics",
        description="Patient demographic characteristics: age, sex, race, ethnicity, smoking status, year of birth.",
        relevant_tables=["data_clinical_patient.txt", "patient_level_dataset.csv"],
        typical_answer_types=["count", "proportion", "median_with_ci", "categorical"],
        common_ambiguity_sources=[
            "Age 'at what time' (diagnosis vs sequencing vs treatment start vs death)",
            "Race/ethnicity grouping (NAACCR primary race vs derived Race/Ethnicity)",
            "Whether to count unique patients or unique cancers when patients have multiple primaries",
        ],
        guidance=(
            "Demographics questions usually answer in counts/proportions over patients. "
            "An UNAMBIGUOUS question must specify the time-anchor for any age statistic and "
            "whether multiple primaries collapse to the patient or are counted separately."
        ),
    ),
    CategorySpec(
        id=2,
        name="Cancer diagnosis details (stage, grade, histology, metastases)",
        description="Stage at diagnosis (clinical, pathologic, AJCC group), histology, grade, presence and sites of distant metastases at diagnosis.",
        relevant_tables=[
            "data_clinical_patient.txt",
            "cancer_level_dataset_index.csv",
            "data_timeline_cancer_diagnosis.txt",
        ],
        typical_answer_types=["count", "proportion", "categorical"],
        common_ambiguity_sources=[
            "Stage source (Stage at Diagnosis from EHR vs ca_path_stage vs ca_clin_stage vs best_ajcc_stage_cd)",
            "Whether 'metastatic' means de novo (Stage IV at dx, ca_dmets_yn=='Yes') or includes recurrence (dx_to_dmets_mos > 0)",
            "How to handle Stage 'Not applicable' / 'Unknown'",
            "Index cancer only vs all primaries",
        ],
        guidance=(
            "Stage and metastasis status come from MULTIPLE columns with different definitions. "
            "An UNAMBIGUOUS question must commit to one column or coding scheme."
        ),
    ),
    CategorySpec(
        id=3,
        name="Genomic details",
        description="Mutations (SNV/indel), copy-number alterations, fusions, structural variants. Per-patient or per-sample.",
        relevant_tables=[
            "data_mutations_extended.txt",
            "data_CNA.txt",
            "data_cna_hg19.seg",
            "data_fusions.txt",
            "data_sv.txt",
            "cancer_panel_test_level_dataset.csv",
            "data_gene_panel_*.txt",
        ],
        typical_answer_types=["count", "proportion"],
        common_ambiguity_sources=[
            "Population unit (patient with at least one alteration vs sample with alteration)",
            "Variant filter (any vs missense only vs hotspot only vs pathogenic vs ONCOGENIC vs panel-restricted)",
            "Denominator: all patients vs only patients with sequencing on a panel that covers the gene",
            "For CNA: amplification only vs deletion vs both; high-level (|copy| ≥ 2) vs any non-zero call",
            "Which fusion partner counts (gene1 only vs either)",
        ],
        guidance=(
            "Genomic prevalence questions are commonly ambiguous because 'mutated' can mean very "
            "different things. An UNAMBIGUOUS genomic question should pin down: gene, alteration "
            "type/filter, population unit, and denominator (panel coverage)."
        ),
    ),
    CategorySpec(
        id=4,
        name="Treatment history",
        description="Counts and types of regimens received (chemo, targeted, immunotherapy, hormone), line of therapy, drugs administered.",
        relevant_tables=[
            "regimen_cancer_level_dataset.csv",
            "data_timeline_treatment.txt",
            "ca_radtx_dataset.csv",
        ],
        typical_answer_types=["count", "proportion", "median_with_ci", "categorical"],
        common_ambiguity_sources=[
            "Definition of a 'regimen' (regimen_drugs concatenation vs first drug)",
            "Definition of a drug class (e.g., 'platinum' = cisplatin/carboplatin/oxaliplatin? include rare ones?)",
            "Line of therapy ordering (drugs_firstinst flag vs regimen_number ordering vs metastatic-only sequencing)",
            "Inclusion of single-agent vs combinations",
            "Counting unique patients ever receiving vs total regimens administered",
        ],
        guidance=(
            "Treatment counts depend critically on drug-class definitions. An UNAMBIGUOUS treatment "
            "question must enumerate the drug class (or use a single named agent) and define the "
            "line/setting (any line; first-line metastatic; etc.)."
        ),
    ),
    CategorySpec(
        id=5,
        name="Outcomes on treatment (response, PFS, OS)",
        description="Time-to-event outcomes evaluated for a specified treatment context.",
        relevant_tables=[
            "data_clinical_supp_survival.txt",
            "data_clinical_supp_survival_treatment.txt",
            "regimen_cancer_level_dataset.csv",
            "imaging_level_dataset.csv",
        ],
        typical_answer_types=["median_with_ci", "proportion", "hazard_ratio_with_ci"],
        common_ambiguity_sources=[
            "Outcome metric: PFS-I vs PFS-M vs OS",
            "Time-origin: from diagnosis vs from regimen start vs from advanced/metastatic diagnosis",
            "Censoring rule (admin censor at last contact vs last imaging)",
            "Cohort restriction (advanced/metastatic only vs all stages)",
            "Treatment context if not pre-specified",
        ],
        guidance=(
            f"Diagnosis-anchored outcomes available across cohorts: {', '.join(DIAGNOSIS_BASED_OUTCOMES)}. "
            f"Regimen-anchored: {', '.join(REGIMEN_BASED_OUTCOMES)}. "
            "An UNAMBIGUOUS outcome question must specify the metric, the time-origin, and any cohort restriction."
        ),
    ),
    CategorySpec(
        id=6,
        name="Associations between biomarkers and outcomes",
        description="Univariate association between a genomic/biomarker feature and a survival or response endpoint.",
        relevant_tables=[
            "data_mutations_extended.txt",
            "data_CNA.txt",
            "data_fusions.txt",
            "cancer_panel_test_level_dataset.csv",
            "regimen_cancer_level_dataset.csv",
            "pathology_report_level_dataset.csv",  # MSI, MMR, PD-L1
            "data_clinical_supp_survival.txt",
        ],
        typical_answer_types=["hazard_ratio_with_ci", "pvalue", "median_with_ci"],
        common_ambiguity_sources=[
            "Same biomarker-definition issues as category 3 (gene, alteration filter, panel coverage)",
            "Same outcome-definition issues as category 5",
            "Whether to restrict to a particular treatment context",
            "Direction of association (HR > 1 means worse for biomarker-positive group)",
        ],
        guidance=(
            "An UNAMBIGUOUS association question must fully specify the biomarker (Cat 3 disambig) "
            "AND the outcome (Cat 5 disambig) AND the population/treatment context."
        ),
    ),
    CategorySpec(
        id=7,
        name="Associations between treatment and outcomes",
        description="Comparison of outcomes between treatment groups (e.g., regimen A vs B; with vs without targeted therapy).",
        relevant_tables=[
            "regimen_cancer_level_dataset.csv",
            "data_timeline_treatment.txt",
            "data_clinical_supp_survival_treatment.txt",
        ],
        typical_answer_types=["hazard_ratio_with_ci", "median_with_ci", "pvalue"],
        common_ambiguity_sources=[
            "Group definition (any receipt vs first-line vs intent-to-treat from a specified line)",
            "Comparator group definition",
            "Confounding-adjusted vs unadjusted",
            "Outcome metric and time-origin",
        ],
        guidance=(
            "An UNAMBIGUOUS treatment-association question must specify the two arms with explicit "
            "drug-class or regimen definitions, the line/setting, the outcome, and the time-origin."
        ),
    ),
    CategorySpec(
        id=8,
        name="Interactions among treatment and biomarker w.r.t. outcomes",
        description="Three-way: does the treatment effect on outcome differ between biomarker-positive and biomarker-negative patients (interaction term)?",
        relevant_tables=[
            "regimen_cancer_level_dataset.csv",
            "data_mutations_extended.txt",
            "data_CNA.txt",
            "pathology_report_level_dataset.csv",
            "data_clinical_supp_survival_treatment.txt",
        ],
        typical_answer_types=["hazard_ratio_with_ci", "pvalue"],
        common_ambiguity_sources=[
            "All Cat 3, 5, 7 ambiguities apply simultaneously.",
            "Statistical model (Cox PH with interaction; multiplicative vs additive scale)",
            "Subgroup HRs vs interaction p-value as the answer of interest",
            "Power: subgroups may have <10 events → the question may be unanswerable",
        ],
        guidance=(
            "Interaction questions are the most demanding. An UNAMBIGUOUS interaction question must "
            "specify (1) the treatment contrast, (2) the biomarker definition, (3) the outcome and "
            "time-origin, (4) the model (Cox PH with interaction term), and (5) what statistic is "
            "the answer (interaction p-value vs subgroup HRs vs ratio of HRs). If subgroup events "
            "would be < 10 in any cell, the question is effectively under-powered."
        ),
    ),
]


CATEGORIES_BY_ID: dict[int, CategorySpec] = {c.id: c for c in CATEGORIES}


def get(id_: int) -> CategorySpec:
    if id_ not in CATEGORIES_BY_ID:
        raise KeyError(f"Unknown category id {id_}. Known: {sorted(CATEGORIES_BY_ID)}")
    return CATEGORIES_BY_ID[id_]
