"""Canonical concept menu for deterministic ambiguity scoring.

Agents select IDs from this menu.  Gold questions store the same IDs, allowing
the scorer to compare two sets exactly instead of asking an LLM whether two
free-text phrases are semantically equivalent.

``infer_legacy_concept_ids`` is deliberately kept here as a migration aid for
the reviewed workbook's original prose concepts.  New workbook entries should
use the IDs directly; inference is not part of live scoring.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    description: str


CONCEPT_MENU: tuple[Concept, ...] = (
    Concept(
        "ANATOMIC_HISTOLOGIC_SCOPE",
        "Anatomic or histologic scope",
        "Which cancer sites, ca_type values, histologies, or sidedness groups are eligible.",
    ),
    Concept(
        "DISEASE_EXTENT_SCOPE",
        "Disease-extent scope",
        "Which stages or disease extents are included, such as all stages versus Stage IV.",
    ),
    Concept(
        "DISEASE_STATE_DEFINITION",
        "Disease-state definition",
        "How a state such as advanced, metastatic, early, resectable, or mCRPC is defined.",
    ),
    Concept(
        "CLINICAL_SUBGROUP_DEFINITION",
        "Clinical subgroup or category definition",
        "Which clinical or molecular subgroup, subtype, age cutoff, population "
        "stratum, source variable, coding system, grouping, or demographic "
        "dimension applies.",
    ),
    Concept(
        "MISSING_DATA_HANDLING",
        "Missing-data handling",
        "Whether and how unknown, missing, or not-applicable values enter the analysis.",
    ),
    Concept(
        "TREATMENT_DEFINITION",
        "Treatment definition",
        "Which drugs, classes, backbones, combinations, or procedures count as the treatment.",
    ),
    Concept(
        "COMPARATOR_DEFINITION",
        "Comparator definition",
        "Which reference or control group defines the comparison.",
    ),
    Concept(
        "LINE_OF_THERAPY",
        "Line-of-therapy anchor",
        "How first line or another line is anchored and operationalized.",
    ),
    Concept(
        "TREATMENT_SETTING",
        "Treatment or disease setting",
        "The adjuvant, neoadjuvant, metastatic, castration-state, or other treatment setting.",
    ),
    Concept(
        "REGIMEN_SELECTION",
        "Regimen selection",
        "Which qualifying regimen per patient is used and whether monotherapy or combinations qualify.",
    ),
    Concept(
        "CLINICAL_TRIAL_HANDLING",
        "Clinical-trial regimen handling",
        "How regimens with unannotated clinical-trial composition are treated.",
    ),
    Concept(
        "PROCEDURE_OR_TIMING_DEFINITION",
        "Procedure or timing definition",
        "How surgery, adjuvant/neoadjuvant therapy, or a timing window is operationalized.",
    ),
    Concept(
        "GENE_OR_GENE_SET",
        "Gene or gene-set scope",
        "Which gene, genes, or pathway members define the biomarker.",
    ),
    Concept(
        "ALTERATION_TYPE",
        "Alteration-type scope",
        "Which alteration classes count, such as SNV/indel, CNA, fusion, or structural variant.",
    ),
    Concept(
        "VARIANT_INCLUSION_CRITERIA",
        "Variant inclusion criteria",
        "Which variants count by consequence, pathogenicity, hotspot status, or call confidence.",
    ),
    Concept(
        "CNA_THRESHOLD",
        "CNA threshold",
        "Which copy-number threshold defines gain, amplification, loss, or deletion.",
    ),
    Concept(
        "GERMLINE_SOMATIC_SCOPE",
        "Germline versus somatic scope",
        "Whether germline, somatic, or both sources of an alteration count.",
    ),
    Concept(
        "PANEL_COVERAGE",
        "Panel or assay coverage",
        "Whether eligibility is restricted to assays capable of detecting the gene or event.",
    ),
    Concept(
        "SPECIMEN_SELECTION",
        "Genomic specimen selection",
        "Which cancer specimen or sequencing event is used when more than one is available.",
    ),
    Concept(
        "BIOMARKER_TEST_DEFINITION",
        "Biomarker-test definition",
        "Which test, assay result, or operational biomarker definition is used, such as MSI versus MMR.",
    ),
    Concept(
        "OUTCOME_METRIC",
        "Outcome metric",
        "Which endpoint or outcome version is analyzed, such as OS, PFS-I, PFS-M, response, or TTNT.",
    ),
    Concept(
        "RESPONSE_DEFINITION",
        "Response definition",
        "How treatment response is measured or categorized.",
    ),
    Concept(
        "TIME_ORIGIN",
        "Time origin",
        "The date or event from which follow-up time starts.",
    ),
    Concept(
        "TIME_HORIZON",
        "Time horizon",
        "The follow-up horizon or time point at which an outcome is summarized.",
    ),
    Concept(
        "SUMMARY_MEASURE",
        "Summary measure",
        "The descriptive summary, such as mean, median, count, proportion, or survival probability.",
    ),
    Concept(
        "STATISTICAL_ESTIMAND",
        "Statistical estimand",
        "The target contrast or statistic, such as subgroup HRs, an interaction, or a hypothesis test.",
    ),
    Concept(
        "MODEL_SPECIFICATION",
        "Model specification",
        "The statistical model, adjustment covariates, interactions, or test specification.",
    ),
    Concept(
        "CENSORING_RULE",
        "Censoring rule",
        "How ongoing follow-up, competing events, or absent subsequent treatment is censored.",
    ),
    Concept(
        "DELAYED_ENTRY",
        "Delayed entry or left truncation",
        "How risk-set entry after the outcome time origin is handled.",
    ),
    Concept(
        "IMMORTAL_TIME_BIAS",
        "Immortal-time or temporal bias",
        "How post-origin exposure or biomarker ascertainment avoids immortal-time or look-ahead bias.",
    ),
    Concept(
        "ASCERTAINMENT_WINDOW",
        "Ascertainment window or timepoint",
        "When a characteristic is measured, such as at diagnosis versus ever during follow-up.",
    ),
    Concept(
        "REPEATED_OBSERVATIONS",
        "Repeated-observation handling",
        "How multiple regimens, sequential treatments, samples, or observations per patient are handled.",
    ),
    Concept(
        "DENOMINATOR_DEFINITION",
        "Denominator definition",
        "Which eligible or tested population forms a non-panel-related denominator.",
    ),
    Concept(
        "DATA_AVAILABILITY",
        "Data availability",
        "Whether the required variable or measurement exists and is sufficiently populated.",
    ),
)

CONCEPT_BY_ID: dict[str, Concept] = {concept.id: concept for concept in CONCEPT_MENU}
CONCEPT_IDS: tuple[str, ...] = tuple(CONCEPT_BY_ID)


def concept_menu_payload() -> list[dict[str, str]]:
    """JSON-serializable form included in every agent question payload."""
    return [asdict(concept) for concept in CONCEPT_MENU]


def validate_concept_ids(values: list[str]) -> list[str]:
    """Return human-readable validation errors for a concept-ID list."""
    errors: list[str] = []
    unknown = sorted({value for value in values if value not in CONCEPT_BY_ID})
    if unknown:
        errors.append(f"unknown disambiguation concept IDs: {unknown}")
    if len(values) != len(set(values)):
        errors.append("disambiguation concept IDs must be unique")
    return errors


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def infer_legacy_concept_ids(raw: str) -> list[str]:
    """Map one reviewed prose concept to one or more canonical IDs.

    This deterministic mapper exists solely to migrate the 2026-06-19 review
    workbook.  It raises instead of guessing when no rule matches.
    """
    text = " ".join(str(raw or "").strip().lstrip("#").split())
    if not text:
        return []
    if text in CONCEPT_BY_ID:
        return [text]
    bracketed = re.match(r"^\[([A-Z][A-Z0-9_]+)\]", text)
    if bracketed and bracketed.group(1) in CONCEPT_BY_ID:
        return [bracketed.group(1)]

    # The review workbook uses a stable ``concept name (examples...)`` style.
    # Route on that name first so explanatory text such as "not at time-origin"
    # does not accidentally create a second concept.  A few explicitly combined
    # legacy entries intentionally expand to two IDs.
    lead = re.split(r"\s*\(|:\s*", text, maxsplit=1)[0].strip().lower()
    if lead == "outcome metric and time horizon":
        return ["OUTCOME_METRIC", "TIME_HORIZON"]
    if lead == "outcome metric" and "time-origin" in text.lower():
        return ["OUTCOME_METRIC", "TIME_ORIGIN"]
    if lead in {"anti-egfr drug list and line of therapy", "line of therapy / treatment setting"}:
        return (["TREATMENT_DEFINITION", "LINE_OF_THERAPY"]
                if lead.startswith("anti-egfr")
                else ["LINE_OF_THERAPY", "TREATMENT_SETTING"])
    if lead == "alteration scope" and _has(text, r"germline|somatic"):
        return ["ALTERATION_TYPE", "GERMLINE_SOMATIC_SCOPE"]
    if lead == "brca variant filter" and _has(text, r"germline"):
        return ["VARIANT_INCLUSION_CRITERIA", "GERMLINE_SOMATIC_SCOPE"]
    if lead == "pten-loss definition":
        return ["ALTERATION_TYPE", "CNA_THRESHOLD"]

    direct_rules: tuple[tuple[str, str], ...] = (
        (r"^delayed cohort entry|^delayed entry|^left truncation", "DELAYED_ENTRY"),
        (r"^immortal[- ]time", "IMMORTAL_TIME_BIAS"),
        (r"^clinical-trial|^inclusion of clinical-trial", "CLINICAL_TRIAL_HANDLING"),
        (r"^anatomic|^histologic|^cancer-subtype scope|^nsclc histologic|^sidedness grouping|^inclusion of breast sarcoma|^whether to count bladder", "ANATOMIC_HISTOLOGIC_SCOPE"),
        (r"^extent-of-disease|^restriction to metastatic disease|^stage stratum|^disease setting / population scope", "DISEASE_EXTENT_SCOPE"),
        (r"^advanced disease definition|^metastatic-cohort definition|^definition of (advanced|metastatic|early stage|locally advanced)|^resectable cancer definition|^mcrpc definition|^definition of mcrpc|^mcrpc$", "DISEASE_STATE_DEFINITION"),
        (
            r"^subtype restriction|^subtype grouping|^population stratum|"
            r"^age cutoff|^age threshold|^category granularity|"
            r"^race vs ethnicity field choice|^treatment of multi-race coding|"
            r"^demographic dimension",
            "CLINICAL_SUBGROUP_DEFINITION",
        ),
        (r"^handling of (unknown|missing)|^treatment of unknown|^missing/unknown|^denominator handling for unknown", "MISSING_DATA_HANDLING"),
        (r"^chemotherapy backbone|^definition of ['\"]?(chemotherapy|immunotherapy|targeted therapy|folfox-based regimen|gemcitabine-based)|^specific anti-egfr agent|^comparator chemotherapy regimen definition|^first-line treatment class|^whether the cdk4/6|^exclusion of adt", "TREATMENT_DEFINITION"),
        (r"^comparator", "COMPARATOR_DEFINITION"),
        (r"^line[- ]of[- ]therapy|^line of therapy|^first-line anchor|^operational definition of ['\"]?first-line|^definition of ['\"]?first-line|^first regimen for the index|^regimen line", "LINE_OF_THERAPY"),
        (r"^treatment setting|^treatment context|^disease context|^disease setting$|^disease context for docetaxel|^castration-sensitive", "TREATMENT_SETTING"),
        (r"^monotherapy vs combination|^whether the .*regimen may also include", "REGIMEN_SELECTION"),
        (r"^regimen counting unit|^handling of patients with more than one|^patients receiving both agents sequentially", "REPEATED_OBSERVATIONS"),
        (r"^operational definition of adjuvant|^adjuvant regimen window", "PROCEDURE_OR_TIMING_DEFINITION"),
        (r"^.* gene list|^.* gene scope", "GENE_OR_GENE_SET"),
        (r"^alteration types included|^alteration type$|^fgfr3 alteration scope", "ALTERATION_TYPE"),
        (r"^variant |^variant filter|^.* mutation definition|^.* alteration definition|^.* variant definition|^.* variant filter|^tp53 variant inclusion|^fusion call confidence", "VARIANT_INCLUSION_CRITERIA"),
        (r"^cna threshold", "CNA_THRESHOLD"),
        (r"^panel coverage|^denominator restriction to panels|^biomarker-frequency denominator|^denominator$|^denominator definition", "PANEL_COVERAGE"),
        (r"^sample selection|^sample inclusion|^which sequencing event|^genomic-specimen scope", "SPECIMEN_SELECTION"),
        (r"^msi-h definition", "BIOMARKER_TEST_DEFINITION"),
        (r"^outcome metric|^pfs metric|^time-on-treatment metric", "OUTCOME_METRIC"),
        (r"^response definition", "RESPONSE_DEFINITION"),
        (r"^time[- ]origin|^os time-origin|^outcome time-origin|^definition of onset of advanced disease", "TIME_ORIGIN"),
        (r"^summary statistic", "SUMMARY_MEASURE"),
        (r"^answer statistic|^target statistic|^statistical comparison|^statistic$|^estimand$|^interaction p-value", "STATISTICAL_ESTIMAND"),
        (r"^cox ph model specification|^adjustment for confounders", "MODEL_SPECIFICATION"),
        (r"^censoring rule", "CENSORING_RULE"),
        (r"^time anchor for|^timing window|^liver involvement ascertainment|^exposure window", "ASCERTAINMENT_WINDOW"),
        (r"^mmr-deficient denominator", "DENOMINATOR_DEFINITION"),
        (r"^smoking status variable availability", "DATA_AVAILABILITY"),
    )
    for pattern, concept_id in direct_rules:
        if _has(lead, pattern):
            return [concept_id]

    found: list[str] = []

    def add(concept_id: str) -> None:
        if concept_id not in found:
            found.append(concept_id)

    # Population and clinical scope.
    if _has(text, r"anatomic|histolog|ca_type|sidedness|sarcoma vs carcinoma|which .*cancer.*count|full urothelial|nsclc subtyp"):
        add("ANATOMIC_HISTOLOGIC_SCOPE")
    if _has(text, r"extent-of-disease restriction|restriction to metastatic disease|stage stratum|all stages|stage iv only|disease setting / population scope"):
        add("DISEASE_EXTENT_SCOPE")
    if _has(text, r"advanced disease definition|definition of ['\"]?(?:advanced|metastatic|early stage|locally advanced)|metastatic-cohort definition|mcrpc definition|definition of mcrpc|resectable cancer definition|timing window \(at diagnosis vs ever\)"):
        add("DISEASE_STATE_DEFINITION")
    if _has(
        text,
        r"subtype restriction|subtype grouping|population stratum|age cutoff|"
        r"age threshold|defining ['\"]?(?:older|young)|hr\+|tnbc vs|"
        r"clinical subgroup|category granularity|race vs ethnicity field|"
        r"multi-race coding|demographic dimension|variable choice|field choice",
    ):
        add("CLINICAL_SUBGROUP_DEFINITION")
    if _has(text, r"missing|unknown|not applicable") and not _has(text, r"ca_type-missing"):
        add("MISSING_DATA_HANDLING")

    # Treatment definition and selection.
    if _has(text, r"chemotherapy backbone|definition of .*therapy|definition of .*regimen|drug list|specific anti-egfr agent|first-line treatment class|exclusion of adt|cdk4/6-inhibitor regimen|treatment contrast|platinum-based|gemcitabine-based|folfox-based|immunotherapy \("):
        add("TREATMENT_DEFINITION")
    if _has(text, r"comparator|comparison group|reference group"):
        add("COMPARATOR_DEFINITION")
    if _has(text, r"line[- ]of[- ]therapy|line of therapy|first-line anchor|first regimen .* vs first regimen|operational definition of ['\"]?first-line|definition of ['\"]?first-line|regimen line"):
        add("LINE_OF_THERAPY")
    if _has(text, r"treatment setting|disease context|disease setting|castration-sensitive|castration-resistant|treatment context"):
        add("TREATMENT_SETTING")
    if _has(text, r"monotherapy vs combination|regimen selection|regimen inclusion|may also include|first .* regimen per patient"):
        add("REGIMEN_SELECTION")
    if _has(text, r"clinical-trial|clinical trial"):
        add("CLINICAL_TRIAL_HANDLING")
    if _has(text, r"adjuvant regimen window|operational definition of adjuvant|procedure|surgery window"):
        add("PROCEDURE_OR_TIMING_DEFINITION")

    # Genomic definitions.
    if _has(text, r"gene list|gene scope|ddr gene|brca1, brca2|which genes"):
        add("GENE_OR_GENE_SET")
    if _has(text, r"alteration type|alteration scope|mutation vs fusion|snv.*cna|include cnas|mutation, cna, fusion|pten-loss definition"):
        add("ALTERATION_TYPE")
    if _has(text, r"variant |mutation definition|mutation status definition|alteration definition|pathogenic|hotspot|non-silent|non-synonymous|fusion call confidence|variant filter|variant inclusion"):
        add("VARIANT_INCLUSION_CRITERIA")
    if _has(text, r"cna threshold|amplification only vs|cna ==|high-level amplification|any gain"):
        add("CNA_THRESHOLD")
    if _has(text, r"germline|somatic only vs"):
        add("GERMLINE_SOMATIC_SCOPE")
    if _has(text, r"panel cover|panels? covering|coverage denominator|fusion-capable panels|denominator.*panel|only those whose panel|assay.*detect"):
        add("PANEL_COVERAGE")
    if _has(text, r"sample selection|specimen selection|sample inclusion|sequencing event|genomic-specimen scope|multiple ngs samples"):
        add("SPECIMEN_SELECTION")
    if _has(text, r"msi-h definition|mmr.*definition|biomarker test|pathology testing definition"):
        add("BIOMARKER_TEST_DEFINITION")

    # Outcome and statistical specification.
    if _has(text, r"outcome metric|pfs metric|response rate|time-on-treatment metric"):
        add("OUTCOME_METRIC")
    if _has(text, r"response definition|best imaging assessment"):
        add("RESPONSE_DEFINITION")
    if _has(text, r"time[- ]origin|time origin|origin for os|from diagnosis vs|from regimen start vs|outcome time-origin"):
        add("TIME_ORIGIN")
    if _has(text, r"time horizon|follow-up horizon"):
        add("TIME_HORIZON")
    if _has(text, r"summary statistic|summary measure|mean vs median"):
        add("SUMMARY_MEASURE")
    if _has(text, r"answer statistic|target statistic|statistical comparison|statistic \(|estimand|interaction p-value|subgroup hrs|ratio of hrs|test of association"):
        add("STATISTICAL_ESTIMAND")
    if _has(text, r"model specification|cox ph model|covariate|adjustment for confounders"):
        add("MODEL_SPECIFICATION")
    if _has(text, r"censoring rule"):
        add("CENSORING_RULE")

    # Timing, bias, repeated observations, and denominator/data issues.
    if _has(text, r"delayed cohort entry|left truncation|left-truncated"):
        add("DELAYED_ENTRY")
    if _has(text, r"immortal[- ]time|exposure ascertainment relative|look-ahead|landmark"):
        add("IMMORTAL_TIME_BIAS")
    if _has(text, r"time anchor for age|time anchor for the age|ascertainment \(at diagnosis|at diagnosis vs anytime|exposure window|timing window"):
        add("ASCERTAINMENT_WINDOW")
    if _has(text, r"more than one .*regimen|count every .*regimen|patients receiving both agents sequentially|multiple regimens|repeated observation"):
        add("REPEATED_OBSERVATIONS")
    if _has(text, r"denominator") and "PANEL_COVERAGE" not in found:
        add("DENOMINATOR_DEFINITION")
    if _has(text, r"variable availability|data availability|not populated|no surgery dataset"):
        add("DATA_AVAILABILITY")

    if not found:
        raise ValueError(f"no deterministic concept-ID mapping for legacy concept: {raw!r}")
    return found


def infer_legacy_concept_list(raw_concepts: list[str]) -> list[str]:
    """Map and de-duplicate a question's prose concepts, preserving menu order."""
    selected: set[str] = set()
    for raw in raw_concepts:
        selected.update(infer_legacy_concept_ids(raw))
    return [concept_id for concept_id in CONCEPT_IDS if concept_id in selected]
