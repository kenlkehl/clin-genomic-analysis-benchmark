# Instructions for agents evaluated on the clin-genomic-analysis-benchmark

You are an LLM agent being evaluated on the Clinical Genomic Analysis Benchmark, which measures performance on clinico-genomic data analyses in oncology. The core tasks are (1) judging whether each of four components of a research question — the cohort denominator, the outcome, the independent variables and covariates, and the statistical analysis plan — is specified clearly enough, given the BPC data dictionary directives and the structure of the dataset, to yield one unambiguous analysis pipeline; and (2) performing the analysis when every component is clear. The dataset comes from the AACR GENIE Biopharma Collaborative (BPC), which includes longitudinal, granular clinical annotations of cancer diagnosis, treatments, outcomes, and cancer panel (NGS) test specimens and results.

This document describes what the benchmark expects an agent under evaluation to do, and the conventions it should follow when judging questions and computing answers.

The benchmark serves each clinical question in two stages: **assess** and **analyze**. The agent receives the question text, the cohort directory (read-only), a data dictionary, and a scratch directory for intermediate files. The cohort directory also holds the BPC Analytic Data Guide, a PDF whose file name contains "Analytic Data Guide"; it defines the derived variables, including the event and censoring rules of the canonical endpoints. Outputs are JSON objects with the fixed schemas below; the harness rejects output that does not match them.

---

## How to score highly (quick checklist)

1. **Judge each component separately.** For the denominator, the outcome, the covariates, and the analysis plan, ask: could two careful analysts who follow this document make different choices in building this component? If so, it needs clarification.
2. **Look for every choice the question leaves open.** Each component below lists concepts that a question often has to pin down. They are examples, not an exhaustive checklist.
3. **Judge from the wording, not the data.** A choice the question leaves open needs clarification even if every reading would give the same result in this cohort (see "What counts as a gap").
4. **Apply the conventional defaults first.** A choice a default settles is not a gap. Never flag anything listed under "Avoid flagging on these grounds alone".
5. **Flag each gap once**, in the component where the construct plays its role in the question.
6. **When every component is clear, compute deterministically** to perform analysis, follow every rule the question states, and return only the typed JSON (see "Answer-type schemas" and "Output discipline").

Each component is scored on its own with balanced accuracy, so flagging a component that is clear costs as much as missing one that is not.

---

## The two stages

### 1. assess

For each of the four components, decide whether the question needs clarification before one analysis pipeline follows from it. The question is ambiguous if any component needs clarification, in which case it goes no further. If every component is clear, the harness moves on to analyze.

You may read the data dictionary and the Analytic Data Guide, and you may inspect the data files to learn which variables exist and how they are coded. Whether a component needs clarification does not depend on whether competing readings give different results in this cohort. Do not search for an answer key; such attempts are monitored and will result in a zero score.

**Output** (all four keys are required):

```json
{ "denominator":   { "needs_clarification": false, "rationale": "<one sentence>" },
  "outcome":       { "needs_clarification": true,  "rationale": "<one sentence>" },
  "covariates":    { "needs_clarification": false, "rationale": "<one sentence>" },
  "analysis_plan": { "needs_clarification": false, "rationale": "<one sentence>" } }
```

A component the question does not involve (for example, covariates in a single-group count) does not need clarification. Each `rationale` says what is unclear, or how the question or a convention specifies the component. Rationales are recorded but not scored.

### 2. analyze

Only invoked if every component was judged clear. Compute the answer using the cohort data files. Use Python (pandas, numpy, scipy, statsmodels, lifelines as needed). The cohort directory is read-only; write any intermediate files into the scratch directory.

**Output:** see [Answer-type schemas](#answer-type-schemas) below.

---

## Judging clarity

The standard for every component is the same: given this document's conventions and the BPC data dictionary and Analytic Data Guide, does the question fix every choice needed to build this part of the analysis? Imagine writing the analysis code with access to the data dictionary and directives but before seeing any data: every choice that code would have to make must be fixed by the question or by a conventional default. A component can be clear even if building it takes several deterministic steps.

### What counts as a gap

Each component below lists concepts that a question often has to pin down for that component to be clear. They are examples of what an unambiguous research question specifies, not an exhaustive checklist: any other choice an analyst would have to make in building the component is judged the same way. A listed concept that cannot arise in the question (for example, left truncation when there is no time-to-event outcome) is not a gap.

Judge gaps from the question's wording, this document's conventions, and the data dictionary, not from what the cohort data happen to contain. If the question leaves a choice open and no conventional default settles it, the component needs clarification, even if every reading would give the same result in this cohort. For example, a question that groups or measures records by a variable that can be unknown or missing must say how those values are handled, whether or not any analysed record in this cohort has one (see "Missing values" under each component).

### Where a gap belongs

Assign each gap to the component where the construct plays its role in the question:

- it decides who or what is eligible → **denominator**;
- it is what is measured or counted → **outcome**;
- it defines an exposure, comparison group, stratifier, or adjustment variable → **covariates**;
- it decides how the estimate is computed → **analysis plan**.

The same term can sit in different components in different questions. For example, the term "Metastatic" restricts the denominator in "among patients with metastatic disease, …", is the outcome in "what proportion had metastatic disease", and is a covariate in "compare survival between patients with and without metastatic disease". The rules for a construct apply wherever it appears.

Flag a gap once, in the component that defines the construct. Do not also flag a component that only inherits it: an outcome measured "from the start of first-line therapy" is not unclear just because the denominator's definition of first-line therapy is. Genomic testing timing (denominator) and left truncation (analysis plan) are two different gaps, not one gap seen twice; see those concepts.

---

## The four components

### 1. Cohort denominator

Which patients, or which regimens, specimens, or cancers, are analysed. The overriding question is whether the denominator is specified clearly enough to yield one analysis pipeline.

**Concepts to pin down** (examples, not an exhaustive list):

- **Disease burden.**
  - Whether a stage term such as "Stage IV", "Stage I–III", "early stage", "localized", or "locally advanced" means stage at initial diagnosis (de novo, newly diagnosed), or also covers patients who reached that stage later.
  - Whether "advanced" or "metastatic" includes only de novo advanced disease (Stage IV at diagnosis), or also relapsed or recurrent advanced disease after an earlier-stage diagnosis.
- **Line of therapy.** For terms such as "first-line", "second-line", "later-line", "previously treated", or "treatment-naive":
  - the disease state the line refers to, for example the first regimen for the cancer in any setting versus the first regimen given for advanced or metastatic disease; and
  - when the line refers to advanced disease that is not exclusively de novo, how treatment given earlier in the early-stage setting (neoadjuvant, adjuvant, or other curative-intent therapy) counts. Some line definitions settle this: "the first regimen started on or after the date of advanced disease" does not count earlier regimens, and "the first regimen for the cancer in any setting" counts them all. A bare "first-line for metastatic disease" that includes recurrent disease does not settle it.

  A line term qualified by a stage (for example, "first-line therapy for Stage IV disease") also raises the disease-burden concept above; both belong to the denominator.
- **Genomic testing timing.** For a question that touches genomics (a biomarker in the eligibility criteria, an exposure, or the outcome), what to do about patients whose index genomic test (the NGS report for the key index cancer) had not yet happened at a time point that defines eligibility. Such time points include the start of a qualifying regimen, the date of advanced or metastatic disease, the diagnosis date when patients are followed from diagnosis, and any landmark. Acceptable rules include requiring testing before that time point, including patients whatever the testing date, or a stated delayed-entry rule. A stated delayed-entry rule for the outcome's index date (for example, left truncation at the NGS report date) settles this for that time point. The concept applies whenever such a time point is not defined to follow testing, whether or not any patient in this cohort was tested after it. When the time point is also the index date of a time-to-event outcome, this concept and left truncation under the analysis plan are separate gaps: if the question states neither, flag both components. Requiring testing before the index date, or a stated delayed-entry rule, settles both. Including patients whatever the testing date settles eligibility but leaves risk-set entry open.
- **Biomarker definitions for inclusion.** For a denominator that requires the presence or absence of a biomarker, the genes or gene set, panel coverage, alteration types (SNV/indel, copy number, fusion, structural variant), variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, and which specimen counts.
- **Cancer type and histology.** The eligible cancer types and histologies (`ca_type`, histology, primary site, sidedness), and how the question's terms map to their codes. A bare reference to the cohort's own cancer, by name or abbreviation, is settled by the key-index-cancer convention. Note that `ca_type` is largely missing in some cohorts (see the Analytic Data Guide).
- **Unit of analysis.** The unit analysed, and which observation qualifies when a patient can contribute several regimens, specimens, or cancers.
- **Qualifying treatment.** The drugs or regimens that qualify a patient for a treatment-defined cohort, including whether monotherapy or combinations qualify and how clinical-trial regimens with masked drug names are handled.
- **Surgery and treatment timing.** The procedure or timing rule when eligibility depends on surgery, or on adjuvant or neoadjuvant timing.
- **Eligibility subgroups.** Clinical or molecular subgroups used for eligibility (receptor status, biomarker groups, age bands, demographic categories) and their source variables.
- **Missing values.** Eligibility criteria apply only to records known to meet them. A criterion that requires a value (for example, "Stage IV at diagnosis" or "with a mutation in gene X") excludes records whose value is unknown, and a criterion that excludes a value (for example, "excluding patients with brain metastases") keeps them. Unknown values in a variable used only for eligibility are therefore not, by themselves, a gap.

### 2. Outcome

What is measured or counted for each analysed unit.

**Concepts to pin down** (examples, not an exhaustive list):

- **Endpoint.** The endpoint itself, when the wording admits more than one (for example, a "benefit" or "outcome" that could be survival, response, or time to next treatment).
- **Index date.** The time zero of a time-to-event outcome, such as diagnosis, advanced or metastatic disease, regimen start, surgery, sequencing, or another landmark, unless the treated-patient default applies (see "Conventional defaults").
- **Event ascertainment.** How progression, response, recurrence, or relapse is ascertained. BPC curates these from imaging reports (radiologist assessment) and from medical oncologist notes (clinician assessment), and combines them. For progression-free survival, the BPC endpoints are PFS-I (imaging), PFS-M (medical oncologist), PFS-I-or-M (either), and PFS-I-and-M (both). An unqualified "PFS", "progression", "response", "recurrence", or "relapse" leaves the source open.
- **Censoring and competing events.** When the stated endpoint and index date have no canonical BPC variable, the censoring date and whether death or another event counts as an event, a censoring event, or a competing event. For an endpoint with a competing event (for example, time to next treatment when death can come first, or a cumulative incidence), how that event is handled. The canonical BPC endpoint variables carry their own event and censoring rules, so using one settles this; see "Conventional defaults".
- **Outcome categories.** What counts as a response or another categorical outcome, when categories could be grouped differently.
- **Measurement time.** For a characteristic that is measured or counted, when it is measured (at diagnosis, before the index date, within a window, or ever during follow-up).
- **Biomarker definition.** For a biomarker outcome, the genes or gene set, alteration types (SNV/indel, copy number, fusion, structural variant), variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, and which specimen counts.
- **Source variable.** The source variable or coding when the data dictionary records the outcome in more than one place.
- **Missing values.** How analysed units with an unknown or missing outcome value are handled: for example, whether patients with unknown status are dropped from a proportion's denominator or counted as not having the outcome. Canonical endpoint variables settle this through their own population and censoring rules.

### 3. Independent variables and covariates

Every exposure, comparison group, stratifier, and adjustment covariate must be defined precisely enough that each analysed record falls in exactly one category, or takes exactly one value. The disease-burden, line-of-therapy, and genomic-testing concepts under the denominator apply here too when the construct is a covariate.

**Concepts to pin down** (examples, not an exhaustive list):

- **Biomarker exposures.** The genes or gene set, alteration types, variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, which specimen counts when there are several, and the assay and result that define a non-NGS biomarker (for example, receptor status and how equivocal results count).
- **Treatment exposures.** The drugs or classes that count (for example, whether "chemotherapy" means cytotoxic therapy only), whether monotherapy or combinations count, how clinical-trial regimens with masked drug names are handled, and the window in which treatment counts (before the index date, or ever).
- **Clinical covariates.** The variable and its time point (for example, age at diagnosis, at sequencing, or at regimen start), the source variable when the data dictionary has several (for example, separate race and ethnicity fields), and category groupings or cutoffs.
- **Comparison groups.** The reference group for any contrast, and whether there is a comparison group at all when the stated population makes the exposure constant.
- **Measurement time.** When a characteristic is measured (at diagnosis, before the index date, within a window, or ever during follow-up).
- **Missing values.** How records with an unknown or missing value of an exposure, comparison group, stratifier, or covariate are handled: for example, a separate category, exclusion, or another stated rule.

### 4. Statistical analysis plan

How the estimate is computed from the denominator, outcome, and covariates.

**Concepts to pin down** (examples, not an exhaustive list):

- **Statistical estimand.** The quantity to report, such as a hazard ratio, a median, a difference in medians, a survival probability at a stated time, a proportion, an odds ratio, a p-value, or subgroup-specific estimates versus an interaction. "Compare survival between A and B" leaves the estimand open.
- **Model architecture.** Which model or test produces the estimand when more than one could: for example, Cox proportional hazards, a stratified Cox model, a time-varying exposure, a landmark analysis, logistic regression, a log-rank test, or a chi-square versus Fisher's exact test. The conventions below fix some of these, including the unadjusted Cox model for a hazard ratio and the Kaplan–Meier median.
- **Variables in a multivariable model.** Which variables to include when the question asks for adjustment ("adjusted", "controlling for", "multivariable"). Their definitions are judged under covariates.
- **Left truncation (delayed entry).** BPC includes only patients whose tumors were sequenced, so time before sequencing is selectively observed. For a time-to-event outcome whose index date can precede the NGS report (any index date other than sequencing itself or a date defined to follow it), how patients enter the risk set: for example, left truncation at the NGS report date, restriction to patients sequenced before the index date, or an explicit choice to ignore delayed entry. This applies to every time-to-event question, genomic or not, and whether or not any analysed patient in this cohort was sequenced after the index date. Using a canonical endpoint variable does not settle it. For a question that touches genomics, it is a separate gap from genomic testing timing under the denominator (see that concept).
- **Exposure ascertained after the index date.** When an exposure or biomarker status can be ascertained after the index date (for example, ever receiving a drug, or a biomarker from a test that can follow the index date), how that time is handled: a landmark, a time-varying exposure, an exposure defined before the index date, or left truncation at the testing date. A stated rule of left truncation at the testing date settles this for biomarker status from that test.

---

## Conventional defaults

When a question uses any of these terms without further qualification, assume the convention listed. A choice these defaults settle is not a gap.

- **"non-synonymous mutation"** → MAF `Variant_Classification` ∈
  {Missense_Mutation, Nonsense_Mutation, Frame_Shift_Del, Frame_Shift_Ins,
  Splice_Site, In_Frame_Del, In_Frame_Ins, Nonstop_Mutation,
  Translation_Start_Site}. Excludes Silent, intronic, UTR, RNA, IGR,
  Splice_Region.

- **"mutation in gene X"** → any non-synonymous (above) somatic variant in
  `data_mutations_extended.txt` with `Hugo_Symbol == X`.

- **Patient-level mutation aggregation** → patient is positive if ANY of
  their sequenced samples carries the qualifying variant (any-positive
  aggregation).

- **"panel covers gene X"** → use `data_gene_matrix.txt` (or the panel
  column in `data_clinical_sample.txt`) to identify which patients/
  samples were tested on a panel including gene X. Patients without sequencing of a given gene are excluded (not treated as
  wild-type) when the exposure is a mutation.

- **Copy-number profiling** → a sample is copy-number profiled only if it
  has a column in `data_CNA.txt`; some assays report no copy-number data
  even though their gene panels list the gene. Biomarkers defined wholly
  or partly by copy number are assessed only in patients/samples with
  copy-number data; exclude the others, as unsequenced patients are
  excluded for mutation exposures.

- **"regimen containing drug X"** → any row in
  `regimen_cancer_level_dataset` whose `regimen_drugs` string contains
  drug X (case-insensitive substring), including regimens with additional
  concomitant drugs. Treat biosimilars as identical to the reference
  molecule, but treat antibody-drug conjugates with the same antibody but
  different payloads as distinct drugs. Biosimilar / prodrug / drug-class
  granularity is not, by itself, a gap. The same substring rule applies to
  "regimen containing X and Y" (both drugs present, other drugs allowed).
  A combination named without "containing", such as "X plus Y" or "X with
  Y", leaves open whether regimens with additional drugs qualify. Naming an
  optional drug ("X plus Y, with or without Z") settles only Z; whether
  regimens that add any other drug qualify stays open. Only wording that
  excludes other drugs ("only", "exactly", "and no other drugs") or allows
  them ("containing", "with or without other drugs") settles it.

- **"Assume any clinical-trial regimen did not contain drug X"** (or drugs
  X, Y, …) → treat every regimen with `drugs_ct_yn == 'Yes'` as not
  containing X (or any of the named drugs), even when
  its `regimen_drugs` string names X. Trial rows often list some drugs and
  mask others (e.g. "Investigational Drug").

- **Regimen attribution** → unless the question explicitly says otherwise,
  count only regimens administered for the patient's **key index cancer**
  (see below); do not count regimens given for a non-index cancer or for a
  different (non-key) index cancer. Which index cancer a regimen/specimen
  belongs to is resolved by the key-index-cancer convention and is not, by
  itself, a gap.

- **Hazard ratio / Cox model** → a hazard ratio means an unadjusted
  (univariable) Cox proportional-hazards model from `lifelines`, unless the
  question names covariates, says "adjusted", or specifies another model
  structure. 

- **"NGS testing date"** → the patient's earliest sequencing report date
  for the relevant cancer (`dx_cpt_rep_days` in
  `cancer_panel_test_level_dataset.csv`). `dx_cpt_rep_days` counts days
  from diagnosis of the cancer; to place it on another time scale, subtract
  that origin's days from diagnosis (for regimen start,
  `dx_reg_start_int` in `regimen_cancer_level_dataset.csv`). When
  left-truncating, a patient enters the risk set at that date on the
  outcome's time scale, or at the time origin if sequencing came first.

- **Outcomes of treated patients** → when every patient analysed has a
  qualifying regimen (one regimen group, regimens compared with each
  other, or groups compared among recipients of a regimen), time-to-event
  outcomes are measured from the start of that regimen unless the question
  says otherwise.

- **Median with 95% CI for time-to-event** → Kaplan–Meier median from
  lifelines `KaplanMeierFitter` (default `alpha=0.05`), with the CI from
  `median_survival_times(kmf.confidence_interval_)`: the Brookmeyer–Crowley
  method applied to lifelines' default log(-log) pointwise band.

- **Median with 95% CI for non-survival continuous variable (e.g., age)**
  → exact distribution-free CI (binomial order-statistic), or equivalently
  the lifelines/scipy default.

- **Stage at diagnosis** → `stage_dx`, BPC's stage at diagnosis, when a
  question restricts or groups by stage at diagnosis. A stage term that
  does not say "at diagnosis", "de novo", or "newly diagnosed" leaves the
  disease-burden concept open (see "Disease burden").

- **"Survival"** → overall survival, when the question does not name
  another endpoint.

- **"OS"** → use the canonical OS variable for the stated index date when relevant
  (`tt_os_g_*` from regimen start, `tt_os_dx_*` from diagnosis,
  `tt_os_adv_*` from advanced-disease diagnosis, etc.). If there is no
  canonical variable for the index date, derive OS from the same death and
  last-known-alive dates, censoring at last known alive.

- **Canonical endpoint variables** → the BPC OS and PFS variables carry
  their own event and censoring rules, set out in the Analytic Data Guide.
  Death is an event for OS and for every PFS variant. OS, and PFS from
  advanced-disease diagnosis (`*_adv_*`), censor at the last known alive
  date. PFS from regimen start (`*_g_*`) censors at the start of the next
  cancer-directed regimen, or at the last known alive date if there is
  none. PFS variables are populated only for cancers that were stage IV at
  diagnosis or that later developed distant metastasis. Using the canonical
  variable for the stated endpoint and index date settles censoring, but
  not how patients enter the risk set (see "Left truncation").

- **Key index cancer** → restrict analyses to index cancers in
  `cancer_level_dataset_index.csv` (`redcap_ca_index == 'Yes'`). Unless a
  question *explicitly* asks about multiple index cancers, derive a single
  **key index cancer** per patient = the **earliest-diagnosed genomically
  profiled index cancer that meets the question's cancer-level eligibility
  criteria** (for example stage at diagnosis, histology, or a qualifying
  regimen given for that cancer). A genomically profiled index cancer is one
  with an associated panel test in `cancer_panel_test_level_dataset.csv`
  (joined on `record_id` + `ca_seq`); order by `dob_ca_dx_days` where
  present, otherwise `ca_seq`. A later, separate primary that meets the
  criteria therefore makes the patient eligible even when an earlier index
  cancer does not, unless the question excludes patients with prior primary
  cancers. When the question has no cancer-level criteria, the key index
  cancer is the earliest genomically profiled index cancer; in these cohorts
  every patient's earliest index cancer is genomically profiled, so this is
  just the earliest index cancer. All per-patient analyses — population counts, treatment
  questions, and genomic questions (mutations / CNAs / fusions are read from
  the genomic specimen(s) belonging to the key index cancer, i.e. the
  `cpt_genie_sample_id`(s) for that `record_id` + `ca_seq`) — focus on the
  key index cancer. The existence of multiple index cancers, and the
  attribution of a regimen or specimen to an index cancer, are resolved by
  this convention and are **not**, by themselves, gaps. (Which `ca_type` or
  histology counts as an eligible index cancer is a separate matter and can
  still be a denominator gap.) Question stems will generally NOT contain the
  word "index"; a bare reference to "<cohort> cancer", by the cohort's name
  or its usual abbreviation (e.g. "patients in the prostate cancer cohort",
  "patients with colorectal cancer", "patients with NSCLC" in the NSCLC
  cohort), still means each patient's key index cancer. It is not a filter
  on `ca_type`, histology, or OncoTree code.

- **Genomic-specimen scope** → genomic facts (mutations / CNAs / fusions)
  must come from specimens that belong to the cohort's cancer type (an index
  cancer of the cohort); a specimen sequenced for an unrelated other primary
  cancer never counts. For *patient-level* genomic questions ("does the
  patient's tumor have X", "what fraction of patients have X"), read the
  genomic specimen(s) of the patient's **key index cancer**. For
  *sample-level* questions that count specimens ("how many samples / unique
  sequenced samples have X"), include **all** specimens belonging to any of
  the patient's cohort-type index cancers (not only the key one). Phrasings
  such as "patients whose tumors were sequenced" mean patients who have such
  a cohort-cancer specimen.

- **Patient-level attributes** → questions about patient-level attributes
  (sex, race, ethnicity, vital status) are computed over
  `patient_level_dataset.csv`, one row per patient. For such patient-level
  questions, anatomic / histologic cohort scope (`ca_type`, histology,
  behavior code, sub-type inclusion) does not apply and is not a gap.

---

## Avoid flagging on these grounds alone

- Choice of CI method for a median or proportion (use the convention).
- `Variant_Classification` subset (use the convention).
- Unadjusted vs. adjusted Cox (use unadjusted unless the question names
  covariates, says "adjusted", or specifies another model structure).
- Multi-sample patient aggregation (use any-positive).
- "Contains" drug match (use substring). This does not settle "X plus Y"
  wording (see "regimen containing drug X").
- Multiple index cancers per patient (use the key index cancer).
- Whether a regimen or genomic specimen "belongs to an index cancer"
  (resolved by the key-index-cancer convention).
- Biosimilar / prodrug / drug-class granularity (treat biosimilars as the
  reference molecule; exclude distinct-INN ADCs).
- Unknown values of a variable used only for eligibility (criteria apply
  only to records known to meet them; see "Missing values" under the
  denominator).
- Anatomic / histologic cohort scope for patient-level demographic
  questions (they are answered at the patient level).
- Which canonical BPC variable to use for a stated endpoint and index date,
  or censoring for that variable (use the canonical variable). This does
  not cover left truncation.

## Data that cannot support the question

When the construct a component needs has no clearly identified variable in the data, or several plausible proxies, that component needs clarification. A question whose components are all clear but whose estimand cannot be estimated from the data is not a clarity problem: judge every component clear and return the structured `unanswerable` result described under "Answer-type schemas".

---

## File-format reminders

- **`data_clinical_*.txt`** — TAB-DELIMITED, with leading `#`-prefixed
  metadata rows (four in the patient and sample files, five in the
  supplementary survival files). Use
  `pd.read_csv(..., sep='\t', comment='#', low_memory=False)`. The first
  non-`#` row carries machine-name column IDs (PATIENT_ID, CA_GRADE,
  etc.).

- **`data_timeline_*.txt`** — TAB-DELIMITED, no `#` rows.

- **Derived endpoints** — use the analytic datasets (`*_dataset*.csv`) and
  the variables the data dictionary and Analytic Data Guide define. The
  cBioPortal `data_clinical_supp_survival*.txt` files repeat some endpoints
  in another format and are not the canonical source.

- **`data_mutations_extended.txt`**, **`data_CNA.txt`**,
  **`data_fusions.txt`**, **`data_sv.txt`** — TAB-DELIMITED, no `#` rows.

- **`*_dataset*.csv`** — comma-delimited, properly quoted.

---

## Answer-type schemas

Return the answer type the question calls for. All numeric
fields are JSON numbers; integer counts are JSON integers.

If every component is clear but the requested estimand cannot be
estimated from the supplied data, judge every component clear in assess,
then in analyze return the `answer_type` the question calls for, with an `answer` of this
form:

```json
{ "unanswerable": true,
  "value": <placeholder number>,
  "unanswerable_reason": "<specific data limitation>" }
```

`value` is a required placeholder number.

| `answer_type`              | Required `answer` fields |
|---|---|
| `count`                    | `value` (int), `n` (int) |
| `proportion`               | `value` (float), `numerator` (int), `denominator` (int), `ci_low` (float), `ci_high` (float) — Wilson CI by default |
| `median_with_ci`           | `value` (float), `ci_low` (float), `ci_high` (float), `n_total` (int), `n_events` (int) |
| `hazard_ratio_with_ci`     | `value` (float), `ci_low` (float), `ci_high` (float), `p_value` (float), `n_total` (int), `n_events` (int) — Cox PH from lifelines |
| `odds_ratio_with_ci`       | `value` (float), `ci_low` (float), `ci_high` (float), `p_value` (float), `n_total` (int) |
| `pvalue`                   | `value` (float), `test_name` (str), `n_total` (int) |
| `categorical`              | `value` (str), `n_total` (int) |
| `categorical_distribution` | `proportions_by_category` (object: category name → float), `counts_by_category` (object: category name → int), `denominator` (int) |

For `categorical_distribution`, use the category labels exactly as the question
names them, and cover every category it lists — scoring compares your category
set against the reference set and a mismatch scores zero. Proportions are of
`denominator`, so they should sum to ~1.0 unless the question asks otherwise.
Each category is scored separately and the **worst-fitting one sets the grade**,
so a small category estimated badly is not offset by large ones estimated well.

```json
{ "answer_type": "categorical_distribution",
  "answer": {
    "denominator": 200,
    "proportions_by_category": {"Category A": 0.50, "Category B": 0.30,
                                "Category C": 0.20},
    "counts_by_category":      {"Category A": 100, "Category B": 60,
                                "Category C": 40}
  }
}
```

**Full `analyze` output:**

```json
{
  "answer_type": "<one of the types above>",
  "answer": { ...typed fields... },
  "methods": "<brief description of the method, columns used, and any cohort filters>",
  "supporting_evidence": { "rows_used": <int>, "notes": "<optional>" }
}
```

---

## Output discipline

For every stage, reply with ONLY the required JSON object on the LAST
message. Do not wrap it in prose; do not append commentary after it. The
harness extracts JSON from the final assistant turn (tolerating Markdown
fences) and will reject responses with extra trailing text.
