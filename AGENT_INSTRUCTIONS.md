# Instructions for agents evaluated on the clin-genomic-analysis-benchmark

You are an LLM agent being evaluated on the Clinical Genomic Analysis Benchmark, which measures performance at interpreting, disambiguating, and performing clinico-genomic data analyses in oncology.

This document describes what the benchmark expects an agent under
evaluation to do, and the conventions it should follow when computing
answers. 

The benchmark serves each clinical question to the agent in three stages:
**classify**, **disambiguate**, and **analyze**. The agent receives the
question text, the cohort directory (read-only), a data dictionary, and a
scratch directory for intermediate files. Outputs are JSON objects with
the fixed schemas below; the harness validates them and discards anything
extra.

---

## How to score highly (quick checklist)

The benchmark rewards three things: (a) correctly distinguishing a genuinely ambiguous question from
a conventionally answerable one, (b) naming the *material* gaps when a question is ambiguous, and
(c) computing the conventional answer exactly when it is unambiguous. To maximize your score:

1. **Default to UNAMBIGUOUS** when a competent BPC analyst would reach for one column, one filter, and
   one method without asking (see "Default rule for classification"). Flag **AMBIGUOUS** only for a
   *material* choice that competent analysts using the conventions below would still resolve
   differently — and would *materially* change the result.
2. **Apply every "Conventional default" before deciding.** Most apparent gaps are already resolved
   there (key index cancer, genomic-specimen scope, biosimilars, "contains" drug match, any-positive
   aggregation, CI methods, OS/time-origin variables, patient-level attributes). Never flag anything
   listed under "Avoid flagging … on these grounds alone".
3. **The key index cancer is assumed.** A question that says "patients with <cohort> cancer" means
   each patient's key index cancer; stems will usually NOT contain the word "index". Genomic and
   treatment facts come from that cancer (see "Genomic-specimen scope").
4. **Flag AMBIGUOUS only for a trigger in "What still counts as AMBIGUOUS"** (e.g. unanchored
   line-of-therapy, undefined outcome metric, missing/underivable comparator, cancer-level
   anatomic/histologic scope in a heterogeneous cohort, biomarker-frequency denominator, unaddressed
   delayed cohort entry or immortal time bias, missing time origin or filter cut-off). Then select
   only the applicable IDs from the supplied concept menu. Correct IDs earn credit and incorrect IDs lose credit.
5. **When unambiguous, compute deterministically and return only the typed JSON** (see "Answer-type
   schemas" and "Output discipline"): use the conventional statistic, exclude unsequenced patients
   for mutation exposures, and adjust for left truncation whenever the time-origin precedes sequencing.

---

## The three stages

### 1. classify

Decide whether the question is **UNAMBIGUOUS** (two careful analysts
working from this dataset and standard oncology biostatistics conventions
would compute the same answer) or **AMBIGUOUS** (the question leaves a
material analytic choice underspecified that two competent analysts could
reasonably resolve in different ways, materially changing the result).

It is fine to inspect the data dictionary or peek at file headers to make
an informed decision.

**Output:**

```json
{ "classification": "ambiguous" | "unambiguous",
  "rationale": "<one or two sentences>" }
```

### 2. disambiguate

Only invoked if the agent classified the question as AMBIGUOUS. Select the
concrete concepts a questioner would need to specify to make the question
deterministically answerable from the cohort data. Use only IDs from the
`disambiguation_concept_menu` supplied in `question.json`. Select every
material unresolved concept, but do not select merely related concepts:
exact gold IDs earn points and non-gold IDs incur a false-positive penalty.

**Output:**

```json
{ "concept_ids": ["OUTCOME_METRIC", "TIME_ORIGIN"] }
```

### Disambiguation concept menu

The same menu is included as structured data in every `question.json`.

| ID | Select when the question needs... |
|---|---|
| `ANATOMIC_HISTOLOGIC_SCOPE` | eligible cancer sites, `ca_type` values, histologies, or sidedness groups |
| `DISEASE_EXTENT_SCOPE` | a stage or extent restriction, such as all stages versus Stage IV |
| `DISEASE_STATE_DEFINITION` | a definition of advanced, metastatic, early, resectable, or mCRPC disease |
| `CLINICAL_SUBGROUP_DEFINITION` | a clinical/molecular subgroup, subtype, age cutoff, population stratum, source variable, coding system, grouping, or demographic dimension |
| `MISSING_DATA_HANDLING` | a rule for unknown, missing, or not-applicable values |
| `TREATMENT_DEFINITION` | drugs, classes, backbones, combinations, or procedures that count as treatment |
| `COMPARATOR_DEFINITION` | a reference or control group |
| `LINE_OF_THERAPY` | an analytic anchor for first line or another line |
| `TREATMENT_SETTING` | an adjuvant, neoadjuvant, metastatic, castration-state, or other setting |
| `REGIMEN_SELECTION` | which qualifying regimen per patient, or whether mono/combination therapy qualifies |
| `CLINICAL_TRIAL_HANDLING` | a rule for clinical-trial regimens with unannotated composition |
| `PROCEDURE_OR_TIMING_DEFINITION` | an operational definition of surgery/adjuvant/neoadjuvant timing |
| `GENE_OR_GENE_SET` | the genes or pathway members that define a biomarker |
| `ALTERATION_TYPE` | alteration classes such as SNV/indel, CNA, fusion, or structural variant |
| `VARIANT_INCLUSION_CRITERIA` | consequence, pathogenicity, hotspot, or call-confidence filters |
| `CNA_THRESHOLD` | a copy-number threshold for gain/amplification/loss/deletion |
| `GERMLINE_SOMATIC_SCOPE` | whether germline, somatic, or both alterations count |
| `PANEL_COVERAGE` | eligibility based on an assay's ability to detect the gene/event |
| `SPECIMEN_SELECTION` | which specimen or sequencing event to use |
| `BIOMARKER_TEST_DEFINITION` | the assay/result defining a biomarker, such as MSI versus MMR |
| `OUTCOME_METRIC` | the endpoint/version, such as OS, PFS-I, PFS-M, response, or TTNT |
| `RESPONSE_DEFINITION` | how response is measured or categorized |
| `TIME_ORIGIN` | the date/event from which follow-up begins |
| `TIME_HORIZON` | a follow-up horizon or summary time point |
| `SUMMARY_MEASURE` | mean, median, count, proportion, or survival probability |
| `STATISTICAL_ESTIMAND` | the target contrast/statistic, such as subgroup HRs or an interaction |
| `MODEL_SPECIFICATION` | the model, test, covariates, or interaction terms |
| `CENSORING_RULE` | handling of ongoing follow-up or competing/absent events |
| `DELAYED_ENTRY` | risk-set entry after the outcome time origin / left truncation |
| `IMMORTAL_TIME_BIAS` | temporal handling of post-origin exposure or biomarker ascertainment |
| `ASCERTAINMENT_WINDOW` | when a characteristic is measured, such as at diagnosis versus ever |
| `REPEATED_OBSERVATIONS` | multiple regimens, samples, treatments, or observations per patient |
| `DENOMINATOR_DEFINITION` | a non-panel-related eligible or tested denominator |
| `DATA_AVAILABILITY` | whether a required variable/measurement exists and is populated |

### 3. analyze

Only invoked if the agent classified the question as UNAMBIGUOUS. Compute
the answer using the cohort data files. Use Python (pandas, numpy, scipy,
statsmodels, lifelines as needed). The cohort directory is read-only;
write any intermediate files into the scratch directory.

**Output:** see [Answer-type schemas](#answer-type-schemas) below.

---

## Default rule for classification

If a competent BPC analyst would reach for one specific column, one
specific filter, and one specific statistical method without needing to
ask, label **UNAMBIGUOUS** — even if a meta-analyst could imagine
alternative interpretations. Assume the question wants the *conventional*
answer, not the most defensible-under-cross-examination answer. A
question can be unambiguous even if computing it requires multiple
deterministic steps.

Only label **AMBIGUOUS** when the question genuinely leaves a *material*
analytic choice open — one that competent analysts using standard
conventions would still resolve differently.

---

## Conventional defaults

When a question uses any of these terms without further qualification,
assume the convention listed; do **not** treat the term as ambiguous on
these grounds alone.

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
  samples were tested on a panel including gene X.

- **"regimen containing drug X"** → any row in
  `regimen_cancer_level_dataset` whose `regimen_drugs` string contains
  drug X (case-insensitive substring), including regimens with additional
  concomitant drugs. Treat biosimilars as identical to the reference
  molecule (e.g., trastuzumab biosimilars count as trastuzumab), but
  exclude antibody-drug conjugates that have distinct INNs (e.g.,
  trastuzumab emtansine, trastuzumab deruxtecan are *not* "trastuzumab"
  regimens). Biosimilar / prodrug / drug-class granularity is not, by
  itself, a source of ambiguity.

- **Regimen attribution** → unless the question explicitly says otherwise,
  count only regimens administered for the patient's **key index cancer**
  (see below); do not count regimens given for a non-index cancer or for a
  different (non-key) index cancer. Which index cancer a regimen/specimen
  belongs to is resolved by the key-index-cancer convention and is not, by
  itself, a source of ambiguity.

- **Hazard ratio / Cox model** → univariable Cox proportional-hazards
  model from `lifelines`, no covariate adjustment, unless the question
  specifies adjustment. Patients without sequencing are excluded (not
  treated as wild-type) when the exposure is a mutation.

- **Median with 95% CI for time-to-event** → Kaplan–Meier median with
  Brookmeyer–Crowley CI (lifelines default).

- **Median with 95% CI for non-survival continuous variable (e.g., age)**
  → exact distribution-free CI (binomial order-statistic), or equivalently
  the lifelines/scipy default.

- **"OS"** → use the canonical OS variable for the time-origin in
  question (`tt_os_g_*` from regimen start, `tt_os_dx_*` from diagnosis,
  `tt_os_d1_*` from advanced-disease index, etc.) — pick the one matching
  the question's stated origin. If there is no canonical variable for a given anchor date, OS will need to be calculated as a derived entity.

- **"Stage IV"** → `stage_dx == 'Stage IV'` (stage at diagnosis), unless
  the question explicitly says "ever metastatic" or similar.

- **Key index cancer** → restrict analyses to index cancers in
  `cancer_level_dataset_index.csv` (`redcap_ca_index == 'Yes'`). Unless a
  question *explicitly* asks about multiple index cancers, derive a single
  **key index cancer** per patient = the **first (earliest-diagnosed)
  genomically profiled index cancer** — i.e., the earliest qualifying index
  cancer that has an associated panel test in
  `cancer_panel_test_level_dataset.csv` (joined on `record_id` + `ca_seq`;
  order by `dob_ca_dx_days` where present, otherwise `ca_seq`). In these
  cohorts every patient's earliest index cancer is genomically profiled, so
  this is just the earliest index cancer, but state it this way for
  determinism. All per-patient analyses — population counts, treatment
  questions, and genomic questions (mutations / CNAs / fusions are read from
  the genomic specimen(s) belonging to the key index cancer, i.e. the
  `cpt_genie_sample_id`(s) for that `record_id` + `ca_seq`) — focus on the
  key index cancer. The existence of multiple index cancers, and the
  attribution of a regimen or specimen to an index cancer, are resolved by
  this convention and are **not**, by themselves, sources of ambiguity.
  (Histologic / anatomic scope — *which* `ca_type` counts as an eligible
  index cancer — is a separate matter and can still be ambiguous; see "What
  still counts as AMBIGUOUS".) Question stems will generally NOT contain the
  word "index"; a bare reference to "<cohort> cancer" (e.g. "patients in the
  prostate cancer cohort", "patients with colorectal cancer") still means
  each patient's key index cancer.

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
  behavior code, sub-type inclusion) does not apply and is not a source of
  ambiguity. (Anatomic scope can still matter for *cancer-level* questions —
  see "What still counts as AMBIGUOUS".)

---

## Avoid flagging questions as ambiguous on these grounds alone

- Choice of CI method for a median (use the convention).
- `Variant_Classification` subset (use the convention).
- Univariable vs. adjusted Cox (use univariable unless the question says
  "adjusted").
- Multi-sample patient aggregation (use any-positive).
- "Contains" drug match (use substring).
- Multiple index cancers per patient (use the key index cancer).
- Whether a regimen or genomic specimen "belongs to an index cancer"
  (resolved by the key-index-cancer convention).
- Biosimilar / prodrug / drug-class granularity (treat biosimilars as the
  reference molecule; exclude distinct-INN ADCs).
- Anatomic / histologic cohort scope for patient-level demographic
  questions (they are answered at the patient level).

---

## What still counts as AMBIGUOUS

A question is AMBIGUOUS when it leaves at least one of the concepts below
materially underspecified, either in the question text or in light of the
actual cohort data, and none of the conventional defaults above resolves it.
The bullets correspond one-to-one with the IDs in the disambiguation concept
menu. Select the named ID when its condition applies.

- **`ANATOMIC_HISTOLOGIC_SCOPE`** — The eligible cancer sites, `ca_type`
  values, histologies, or sidedness groups are not specified and materially
  different choices would change the analysis population. This commonly
  applies to cancer-level questions in heterogeneous cohorts. Patient-level
  demographic questions remain exempt under the convention above.

- **`DISEASE_EXTENT_SCOPE`** — The question does not specify a required stage
  or extent restriction, such as all stages versus Stage IV, and more than one
  materially different restriction is clinically plausible. Use this for a
  direct stage/extent eligibility choice; use `DISEASE_STATE_DEFINITION` when
  the unresolved issue is how to construct a clinical disease state.

- **`DISEASE_STATE_DEFINITION`** — A state such as advanced, metastatic,
  early, resectable, or mCRPC is used without an operational definition. For
  example, "metastatic" may mean de novo Stage IV only or may also include
  recurrent/relapsed metastatic disease.

- **`CLINICAL_SUBGROUP_DEFINITION`** — A clinical or molecular subgroup,
  subtype, age group, population stratum, source variable, coding system,
  category grouping, or demographic dimension lacks a deterministic
  definition. Examples include an undefined age threshold, an incompletely
  specified receptor subgroup, race versus ethnicity, alternative category
  groupings, or how multi-category records should be represented.

- **`MISSING_DATA_HANDLING`** — The result materially depends on whether
  unknown, missing, or not-applicable values are excluded, included in the
  denominator, treated as negative, or reported as a separate category, and
  the question does not supply a rule.

- **`TREATMENT_DEFINITION`** — The drugs, classes, backbones, combinations, or
  procedures that count as the treatment are not deterministic under the
  conventions above. This includes unresolved drug-class boundaries and an
  unqualified term such as "chemotherapy" that could mean cytotoxic treatment
  only or systemic therapy more broadly.

- **`COMPARATOR_DEFINITION`** — A reference or control group is required but
  is not specified or derivable. For example, a requested hazard ratio may
  have several plausible reference groups, or the stated population may make
  the proposed exposure constant and leave no comparator.

- **`LINE_OF_THERAPY`** — "First-line," "second-line," or another line lacks
  an analytic anchor. Plausible anchors include the first regimen for the
  index cancer, the first regimen after metastatic diagnosis, the first
  systemic or cytotoxic regimen, or the first regimen after a landmark. An
  unqualified line is ambiguous unless the population and setting plus a
  corresponding dataset field make the anchor deterministic.

- **`TREATMENT_SETTING`** — The treatment setting is not fixed, such as
  adjuvant, neoadjuvant, metastatic, castration-sensitive, castration-resistant,
  or another clinically distinct context, and the setting changes which
  treatments or patients qualify.

- **`REGIMEN_SELECTION`** — The question does not say which qualifying regimen
  to use when a patient has multiple candidates, or whether monotherapy,
  combination therapy, or regimens containing additional agents qualify.
  Select this for the regimen-selection rule, not for the definition of a
  drug or class itself.

- **`CLINICAL_TRIAL_HANDLING`** — Clinical-trial regimen rows have unannotated
  composition and could materially affect treatment eligibility or assignment,
  but the question gives no rule. Plausible rules include assuming the drug of
  interest is absent, excluding trial regimens, or treating trial regimens as a
  separate group.

- **`PROCEDURE_OR_TIMING_DEFINITION`** — A procedure or a treatment relative
  to a procedure needs an operational timing rule, such as the window defining
  surgery, adjuvant therapy, or neoadjuvant therapy, and no deterministic
  window or ordering rule is supplied.

- **`GENE_OR_GENE_SET`** — A biomarker refers to a pathway, repair process,
  signature, or gene family without specifying which genes are members. For
  example, "DNA damage repair alteration" requires a concrete gene set.

- **`ALTERATION_TYPE`** — The genomic alteration classes that count are not
  specified, such as SNV/indel mutations, copy-number alterations, fusions, or
  structural variants. A phrase such as "FGFR3 altered" may require this choice.

- **`VARIANT_INCLUSION_CRITERIA`** — The question requires consequence,
  pathogenicity, hotspot, or call-confidence filters that are not resolved by
  the mutation defaults above. Do not select this merely to revisit the
  conventional non-synonymous definition when that default directly applies.

- **`CNA_THRESHOLD`** — A copy-number state such as gain, amplification, loss,
  or deletion is requested without a threshold, and multiple materially
  different thresholds are compatible with the wording.

- **`GERMLINE_SOMATIC_SCOPE`** — The question does not establish whether
  germline alterations, somatic alterations, or both count, and the applicable
  data contain more than one plausible source.

- **`PANEL_COVERAGE`** — Eligibility depends on whether an assay could detect
  the gene or event, but the question does not specify a coverage rule. For a
  biomarker frequency, this commonly means choosing between all sequenced
  patients/samples and only those tested on a panel covering the gene. A plain
  alteration count with no denominator is not ambiguous on this ground alone.

- **`SPECIMEN_SELECTION`** — More than one specimen or sequencing event could
  be used and the defaults above do not determine which one qualifies, such as
  the first, latest, pre-treatment, or disease-state-specific specimen.

- **`BIOMARKER_TEST_DEFINITION`** — The assay or result defining a biomarker is
  unspecified. Examples include whether MSI or MMR status is used, which
  receptor-status test and threshold applies, or how equivocal results count.

- **`OUTCOME_METRIC`** — The endpoint or endpoint version is not fixed, such as
  OS, response, TTNT, PFS-I, PFS-M, PFS-I-or-M, or PFS-I-and-M. In BPC, these
  PFS variants use imaging assessment, oncologist assessment, the earlier of
  the two, or both assessments, respectively.

- **`RESPONSE_DEFINITION`** — "Response" is requested without saying how it is
  measured or categorized, such as RECIST/imaging response, clinical response,
  best response, objective response, or disease-control categories.

- **`TIME_ORIGIN`** — Follow-up could plausibly begin at more than one event
  and the question does not choose among them, such as diagnosis, advanced-
  disease onset, regimen start, surgery, or another landmark.

- **`TIME_HORIZON`** — A follow-up horizon, filter cutoff, or summary time
  point is required but missing. Examples include "long-term survivor" without
  a year threshold or survival probability without a requested time point.

- **`SUMMARY_MEASURE`** — The question does not specify whether to report a
  mean, median, count, proportion, survival probability, or another summary,
  and more than one would reasonably answer the wording.

- **`STATISTICAL_ESTIMAND`** — The target contrast or statistic is unclear,
  such as subgroup-specific hazard ratios versus a treatment-by-biomarker
  interaction, a difference versus a ratio, or an overall versus conditional
  effect.

- **`MODEL_SPECIFICATION`** — The required model, test, covariates, adjustment
  set, or interaction terms are not fixed and materially different choices
  remain after applying the defaults. Do not select this solely to question
  the conventional univariable Cox model when that default directly applies.

- **`CENSORING_RULE`** — Handling of ongoing follow-up, absent events,
  competing events, or treatment changes is required but not specified, and
  plausible censoring choices materially change the time-to-event analysis.

- **`DELAYED_ENTRY`** — The outcome origin can precede genomic testing, but
  the question does not say how patients enter the risk set. BPC inclusion
  requires a genomically tested tumor, so time before testing is selectively
  observed. Plausible rules include left truncation at testing, restriction to
  patients tested before the origin, or an explicit alternative. A vulnerable
  time-to-event question must state its rule to be unambiguous.

- **`IMMORTAL_TIME_BIAS`** — An exposure is defined by something occurring
  after follow-up begins, such as ever receiving a treatment, but the question
  does not specify a landmark, time-varying exposure, pre-origin ascertainment,
  or another temporal strategy. A treated-versus-untreated comparison from a
  common earlier origin otherwise grants the treated group guaranteed
  event-free time before exposure.

- **`ASCERTAINMENT_WINDOW`** — A characteristic or exposure could be measured
  at diagnosis, before the outcome origin, within a fixed window, or ever
  during follow-up, and the question does not say which window applies. This
  identifies *when status is determined*; use `IMMORTAL_TIME_BIAS` as well only
  when the unresolved timing creates that specific bias.

- **`REPEATED_OBSERVATIONS`** — Patients can contribute multiple regimens,
  samples, treatments, or measurements, but the question does not specify the
  analysis unit or how repeated observations are selected or aggregated.

- **`DENOMINATOR_DEFINITION`** — A non-panel-related eligible or tested
  denominator is not specified, and materially different populations could
  serve as the denominator. Use `PANEL_COVERAGE` instead when the unresolved
  denominator is specifically eligibility based on assay coverage.

- **`DATA_AVAILABILITY`** — The requested construct lacks a clearly identified
  and sufficiently populated variable or measurement, leaving multiple
  plausible data sources, proxies, or availability requirements. A completely
  well-specified but unestimable analysis is not ambiguous on this ground; use
  the structured `unanswerable` result described below instead.


---

## File-format reminders

- **`data_clinical_*.txt`** and **`data_timeline_*.txt`** — TAB-DELIMITED,
  with FIVE leading `#`-prefixed metadata rows. Use
  `pd.read_csv(..., sep='\t', comment='#', low_memory=False)`. The first
  non-`#` row carries machine-name column IDs (PATIENT_ID, CA_GRADE,
  etc.).

- **`data_mutations_extended.txt`**, **`data_CNA.txt`**,
  **`data_fusions.txt`**, **`data_sv.txt`** — TAB-DELIMITED, no `#` rows.

- **`*_dataset*.csv`** — comma-delimited, properly quoted.

---

## Answer-type schemas

Return one matching the question's expected answer type. All numeric
fields are JSON numbers; integer counts are JSON integers.

If the question is well specified but the requested estimand cannot be
estimated from the supplied data (for example, separation or an empty model
cell), keep it `unambiguous` and return its expected `answer_type` with an
`answer` of this form:

```json
{ "unanswerable": true,
  "value": 1.0,
  "unanswerable_reason": "<specific data limitation>" }
```

Use an appropriate placeholder `value` (normally the null value for a ratio).

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
    "denominator": 1484,
    "proportions_by_category": {"Left colon": 0.2925, "Right colon": 0.3288,
                                "Rectal": 0.3342, "Other": 0.0445},
    "counts_by_category":      {"Left colon": 434, "Right colon": 488,
                                "Rectal": 496, "Other": 66}
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
