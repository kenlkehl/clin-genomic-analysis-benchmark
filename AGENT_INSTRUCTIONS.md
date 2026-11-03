# Instructions for agents evaluated on the clin-genomic-analysis-benchmark

You are an LLM agent being evaluated on the Clinical Genomic Analysis Benchmark, which measures performance on clinico-genomic data analyses in oncology. The core tasks are (1) judging whether each of four components of a research question — the cohort denominator, the outcome, the independent variables and covariates, and the statistical analysis plan — is specified clearly enough, given this document's conventions, the BPC data dictionary, and the structure of the dataset, to yield one unambiguous analysis pipeline; and (2) performing the analysis when every component is clear. The dataset comes from the AACR GENIE Biopharma Collaborative (BPC), which includes longitudinal, granular clinical annotations of cancer diagnosis, treatments, outcomes, and cancer panel (NGS) test specimens and results.

This document describes what the benchmark expects an agent under evaluation to do, and the conventions it should follow when judging questions and computing answers.

The benchmark serves each clinical question in two stages: **assess** and **analyze**. The agent receives the question text, the cohort directory (read-only), a data dictionary, and a scratch directory for intermediate files. The cohort directory also holds the BPC Analytic Data Guide, a PDF whose file name contains "Analytic Data Guide"; it defines the derived variables, including the event and censoring rules of the canonical endpoints. Outputs are JSON objects with the fixed schemas below; the harness rejects output that does not match them.

---

## How to score highly (quick checklist)

1. **Judge each component separately.** For the denominator, the outcome, the covariates, and the analysis plan, ask: could two careful analysts who follow this document make different choices in building this component? If so, it needs clarification.
2. **Look for every choice the question leaves open.** Each component below lists concepts that a question often has to pin down. They are examples, not an exhaustive checklist.
3. **Judge from the wording, not the data.** A choice the question leaves open needs clarification even if every reading would give the same result in this cohort (see "What counts as a gap").
4. **Apply the conventional defaults first.** A choice a default settles is not a gap. Never flag anything listed under "Avoid flagging on these grounds alone".
5. **Flag a gap in every component it touches**: every component whose part of the analysis cannot be built until the gap is resolved (see "Where a gap belongs").
6. **When every component is clear, compute deterministically** to perform analysis, follow every rule the question states, and return only the typed JSON (see "Answer-type schemas" and "Output discipline").

Each component is scored on its own with balanced accuracy, so flagging a component that is clear costs as much as missing one that is not.

---

## The two stages

### 1. assess

For each of the four components, decide whether the question needs clarification before one analysis pipeline follows from it. The question is ambiguous if any component needs clarification, in which case it goes no further. If every component is clear, the harness moves on to analyze.

You may read the data dictionary and the Analytic Data Guide, and you may inspect the data files to learn which variables exist and how they are coded. Whether a component needs clarification does not depend on whether competing readings give different results in this cohort. Do not search for an answer key; such attempts are monitored and will result in a zero score.

**Output** (all four keys are required):

```json
{ "denominator":   { "needs_clarification": false, "rationale": "<one or two sentences>" },
  "outcome":       { "needs_clarification": true,  "rationale": "<one or two sentences>" },
  "covariates":    { "needs_clarification": false, "rationale": "<one or two sentences>" },
  "analysis_plan": { "needs_clarification": false, "rationale": "<one or two sentences>" } }
```

A component the question does not involve (for example, covariates in a single-group count) does not need clarification. Each `rationale` says what is unclear, or how the question or a convention specifies the component. Rationales are recorded but not scored.

### 2. analyze

Only invoked if every component was judged clear. Compute the answer using the cohort data files. Use Python, run through the shell (pandas, numpy, scipy, statsmodels, lifelines as needed). The cohort directory is read-only; write any intermediate files into the scratch directory.

**Output:** see [Answer-type schemas](#answer-type-schemas) below.

---

## Judging clarity

The standard for every component is the same: given this document's conventions and the BPC data dictionary and Analytic Data Guide, does the question fix every choice needed to build this part of the analysis? Imagine writing the analysis code with access to the data dictionary, the Analytic Data Guide, and this document's conventions but before seeing any data: every choice that code would have to make must be fixed by the question or by a conventional default. A component can be clear even if building it takes several deterministic steps.

### What counts as a gap

Each component below lists concepts that a question often has to pin down for that component to be clear. They are examples of what an unambiguous research question specifies, not an exhaustive checklist: any other choice an analyst would have to make in building the component is judged the same way. A listed concept that cannot arise in the question (for example, left truncation when there is no time-to-event outcome) is not a gap.

Judge gaps from the question's wording, this document's conventions, the data dictionary, and the Analytic Data Guide, not from what the cohort data happen to contain. If the question leaves a choice open and no conventional default settles it, the component needs clarification, even if every reading would give the same result in this cohort. For example, a question that groups or measures records by a variable that can be unknown or missing must say how those values are handled, whether or not any analysed record in this cohort has one (see "Missing values" under each component). Treat a variable as one that can be unknown or missing unless the data dictionary or the Analytic Data Guide says it is always recorded. A recorded value too coarse for the question's grouping (for example, "Stage I-III NOS" when Stage III is grouped apart from Stages I–II) is unknown for that grouping.

### Where a gap belongs

Flag a gap in every component it touches: every component whose part of the analysis cannot be built until the gap is resolved. A construct touches:

- the **denominator** if it decides who or what is analysed;
- the **outcome** if it decides what is measured or counted, or when measurement starts or ends (for example, the index date of a time-to-event outcome);
- the **covariates** if it defines an exposure, comparison group, stratifier, or adjustment variable;
- the **analysis plan** if it decides how the estimate is computed.

The same term can sit in different components in different questions, and in more than one component of the same question. For example, the term "Metastatic" restricts the denominator in "among patients with metastatic disease, …", is the outcome in "what proportion had metastatic disease", and is a covariate in "compare survival between patients with and without metastatic disease". The rules for a construct apply wherever it appears.

A gap that touches several components is flagged in each of them. For example:

- In "overall survival from the start of first-line therapy among patients who received first-line X", an unclear "first-line" decides both who is analysed and when each patient's clock starts: a patient who received X after surgery and again at relapse is analysed under either reading, but from a different start date. So the denominator and the outcome both need clarification.
- A gap that decides only whether a patient is analysed, and not when an analysed patient's clock starts, touches the denominator alone. For example, when a line term is tied to a disease state ("first-line X for metastatic disease", outcome measured from the start of that regimen), the readings differ in who is included, but every reading starts an included patient's clock at the same regimen of X, so the outcome is clear. Flag the outcome only if some patient analysed under two readings would get different start dates.
- When the compared groups do not cover everyone in the stated population (for example, "drug A versus drug B" among all treated patients, where patients who received neither drop out), an unclear group definition decides both which group a patient is in and who is analysed, so the covariates and the denominator both need clarification.
- An unclear definition that changes only which group a patient is in, and not who is analysed, touches only the covariates. If it also decides who counts as tested (for example, whether copy-number changes count, which limits the analysis to copy-number-profiled patients), it touches the denominator too.
- An unstated index date touches the denominator as well as the outcome when some reasonable readings exist only for some patients (for example, onset of advanced disease, or the start of a regimen in a cohort not defined by treatment).
- When a biomarker exposure can be ascertained after the index date and the question gives no timing rule (see "Exposure ascertained after the index date"), the choice can move a patient between groups: for example, a later test may show a mutation that an earlier test did not. So the covariates need clarification as well as the analysis plan. A stated rule that settles that concept settles the covariates too.
- When a question leaves open how unknown values of the outcome or of a covariate (an exposure, comparison group, stratifier, or adjustment variable) are handled, it also leaves open whether those records are analysed. So the denominator needs clarification too, along with the component whose variable is unknown.

Genomic testing timing (denominator) and left truncation (analysis plan) are two different gaps, not one gap seen twice; see those concepts.

---

## The four components

### 1. Cohort denominator

Which patients, or which regimens, specimens, or cancers, are analysed. The overriding question is whether the denominator is specified clearly enough to yield one analysis pipeline.

**Concepts to pin down** (examples, not an exhaustive list):

- **Disease burden.**
  - Whether a stage term such as "Stage IV", "Stage I–III", "early stage", "localized", or "locally advanced" means stage at initial diagnosis (de novo, newly diagnosed), or also covers patients who developed the relevant burden of disease later.
  - BPC records stage only at diagnosis (`stage_dx`). A distant metastasis after a Stage I–III diagnosis is recorded separately (`dmets_stage_i_iii`, and the "Stage I-III with Distant Mets" group of `pfs_cohort`), not as Stage IV. A reading of "Stage IV" that includes such patients is built from those fields. BPC's own use of "Stage IV" for stage at diagnosis does not settle which reading a question means.
  - Similarly, whether "advanced" or "metastatic" includes only de novo advanced disease (Stage IV at diagnosis), or also relapsed or recurrent advanced disease after an earlier-stage diagnosis.
  - Which stage groups "advanced" covers (for example, Stage IV only, or Stages III–IV). "Advanced" does not fix them in any use: as a stage at diagnosis ("diagnosed at an advanced stage"), as a population ("patients with advanced disease"), or as a date ("overall survival from onset of advanced disease", "the first regimen for advanced disease"). BPC's advanced-disease variables (`*_adv_*`) define advanced disease as Stage IV at diagnosis or a distant metastasis after a Stage I–III diagnosis. They apply only when the question defines advanced disease that way.
- **Line of therapy.** For terms such as "first-line", "second-line", "later-line", "previously treated", or "treatment-naive":
  - the disease state the line refers to, for example the first regimen for the cancer in any setting, versus the first regimen given for advanced or metastatic disease; and
  - when the line refers to advanced disease that is not exclusively de novo, how treatment given earlier in the early-stage setting (neoadjuvant, adjuvant, or other curative-intent therapy) counts. Some line definitions settle this: "the first regimen started on or after the date of advanced disease" does not count earlier regimens (which stage groups "advanced" covers is a separate question; see above), and "the first regimen for the cancer in any setting" counts them all. A bare "first-line for metastatic disease" that includes recurrent disease does not settle it. Nor does a line term without a setting, such as "first-line systemic therapy for the cancer": "first-line" is often read as the first treatment for advanced disease, and "for the cancer" can be read as any setting, so the wording leaves open whether early-stage treatment counts.
- A line term qualified by a stage (for example, "first-line therapy for Stage IV disease") also raises the disease-burden concept above; both belong to the denominator, and to the outcome only when they move an analysed patient's start date (see "Where a gap belongs").
- **Genomic testing timing.** For a question that touches genomics (a biomarker in the eligibility criteria, an exposure, or the outcome), what to do about patients whose index genomic test (the NGS report for the key index cancer) had not yet happened at a time point that defines eligibility. Such time points include the start of a qualifying regimen, the date of advanced or metastatic disease, the diagnosis date when patients are followed from diagnosis, and any landmark. Acceptable rules include requiring testing before that time point, including patients whatever the testing date, or a stated delayed-entry rule. A stated delayed-entry rule for the outcome's index date (for example, left truncation at the NGS report date) settles this for that time point. The concept applies whenever such a time point is not defined to follow testing, whether or not any patient in this cohort was tested after it. When the time point is itself left open (for example, an unstated index date), the concept applies unless every reasonable reading of it is defined to follow testing. When the time point is also the index date of a time-to-event outcome, this concept and left truncation under the analysis plan are separate gaps: if the question states neither, flag both components. Requiring testing before the index date, or a stated delayed-entry rule, settles both. Including patients whatever the testing date settles eligibility but leaves risk-set entry open.
- **Biomarker definitions for inclusion.** For a denominator that requires the presence or absence of a biomarker, the genes or gene set, panel coverage, alteration types (SNV/indel, copy number, fusion, structural variant), variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, and which specimen counts.
- **Cancer type and histology.** The eligible cancer types and histologies (`ca_type`, histology, primary site, sidedness), and how the question's terms map to their codes. No conventional default names a source for histology, so a question that restricts by histology must say which source and codes it uses. A bare reference to the cohort's own cancer, by name or abbreviation, is settled by the key-index-cancer convention. Note that `ca_type` is largely missing in some cohorts (see the Analytic Data Guide).
- **Implied restrictions.** Wording that presupposes something not every patient in the stated population has (for example, "during their treatment course" when some patients received no treatment) leaves open whether patients without it are analysed.
- **Unit of analysis.** The unit analysed, and which observation qualifies when a patient can contribute several regimens, specimens, or cancers.
- **Qualifying treatment.** The drugs or regimens that qualify a patient for a treatment-defined cohort, including which drugs a broad drug class covers (see "Drug classes"), whether monotherapy or combinations qualify, and how clinical-trial regimens with masked drug names are handled.
- **Surgery and treatment timing.** The procedure or timing rule when eligibility depends on surgery, or on adjuvant or neoadjuvant timing.
- **Eligibility subgroups.** Clinical or molecular subgroups used for eligibility (receptor status, biomarker groups, age bands, demographic categories) and their source variables.
- **Missing values.** Eligibility criteria apply only to records known to meet them. A criterion that requires a value (for example, "Stage IV at diagnosis" or "with a mutation in gene X") excludes records whose value is unknown, and a criterion that excludes a value (for example, "excluding patients with brain metastases") keeps them. Unknown values in a variable used only for eligibility are therefore not, by themselves, a gap. Unknown values of the outcome or of a covariate are different: when their handling is open, the denominator needs clarification too (see "Where a gap belongs").

### 2. Outcome

What is measured or counted for each analysed unit. The disease-burden and line-of-therapy concepts under the denominator apply here too when the construct is part of the outcome (for example, "what proportion had metastatic disease").

**Concepts to pin down** (examples, not an exhaustive list):

- **Endpoint.** The endpoint itself, when the wording admits more than one (for example, a "benefit" or "outcome" that could be survival, response, or time to next treatment).
- **Index date.** The time zero of a time-to-event outcome, such as diagnosis, advanced or metastatic disease (see "Disease burden" for what these terms cover), regimen start, surgery, sequencing, or another landmark, unless the treated-patient default applies (see "Conventional defaults").
- **Event ascertainment.** How progression, response, recurrence, or relapse is ascertained. BPC curates these from imaging reports (radiologist assessment) and from medical oncologist notes (clinician assessment), and combines them. For progression-free survival, the BPC endpoints are PFS-I (imaging), PFS-M (medical oncologist), PFS-I-or-M (either), and PFS-I-and-M (both). An unqualified "PFS", "progression", "response", "recurrence", or "relapse" leaves the source open.
- **Censoring and competing events.** When the stated endpoint and index date have no canonical BPC variable, the censoring date and whether death or another event counts as an event, a censoring event, or a competing event. For an endpoint with a competing event (for example, time to next treatment when death can come first, or a cumulative incidence), how that event is handled. The canonical BPC endpoint variables carry their own event and censoring rules, so using one settles this; see "Conventional defaults".
- **Outcome categories.** What counts as a response or another categorical outcome, when categories could be grouped differently.
- **Measurement time.** For a characteristic that is measured or counted, when it is measured (at diagnosis, before the index date, within a window, or ever during follow-up).
- **Treatment received.** When the outcome is receipt of a treatment, the drugs or classes that count (see "Drug classes"), how clinical-trial regimens with masked drug names are handled, and the window in which treatment counts.
- **Biomarker definition.** For a biomarker outcome, the genes or gene set, alteration types (SNV/indel, copy number, fusion, structural variant), variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, and which specimen counts.
- **Source variable.** The source variable or coding when the data dictionary records the outcome in more than one place.
- **Missing values.** How analysed units with an unknown or missing outcome value are handled: for example, whether patients with unknown status are dropped from a proportion's denominator or counted as not having the outcome. An open choice here touches the denominator too (see "Where a gap belongs"). Canonical endpoint variables settle this through their own population and censoring rules.

### 3. Independent variables and covariates

Every exposure, comparison group, stratifier, and adjustment covariate must be defined precisely enough that each analysed record falls in exactly one category, or takes exactly one value. The disease-burden, line-of-therapy, and genomic-testing concepts under the denominator apply here too when the construct is a covariate.

**Concepts to pin down** (examples, not an exhaustive list):

- **Biomarker exposures.** The genes or gene set, alteration types, variant filters beyond the non-synonymous default, copy-number thresholds, germline versus somatic scope, which specimen counts when there are several, which tests count when one can follow the index date (see "Exposure ascertained after the index date"), and the assay and result that define a non-NGS biomarker (for example, receptor status and how equivocal results count).
- **Treatment exposures.** The drugs or classes that count (for example, whether "chemotherapy" means cytotoxic therapy only; see "Drug classes"), whether monotherapy or combinations count, how clinical-trial regimens with masked drug names are handled, and the window in which treatment counts (before the index date, or ever).
- **Clinical covariates.** The variable and its time point (for example, age at diagnosis, at sequencing, or at regimen start), the source variable when the data dictionary has several (for example, separate race and ethnicity fields), and category groupings or cutoffs.
- **Comparison groups.** The reference group for any contrast, and whether there is a comparison group at all when the stated population makes the exposure constant.
- **Measurement time.** When a characteristic is measured (at diagnosis, before the index date, within a window, or ever during follow-up).
- **Missing values.** How records with an unknown or missing value of an exposure, comparison group, stratifier, or covariate are handled: for example, a separate category, exclusion, or another stated rule. An open choice here touches the denominator too (see "Where a gap belongs").

### 4. Statistical analysis plan

How the estimate is computed from the denominator, outcome, and covariates.

**Concepts to pin down** (examples, not an exhaustive list):

- **Statistical estimand.** The quantity to report, such as a hazard ratio, a median, a difference in medians, a survival probability at a stated time, a proportion, an odds ratio, a p-value, or subgroup-specific estimates versus an interaction. "Compare survival between A and B" and "is there an association between X and survival" leave the estimand open.
- **Model architecture.** Which model or test produces the estimand when more than one could: for example, Cox proportional hazards, a stratified Cox model, a time-varying exposure, a landmark analysis, logistic regression, a log-rank test, or a chi-square versus Fisher's exact test. The conventions below fix some of these, including the unadjusted Cox model for a hazard ratio and the Kaplan–Meier median.
- **Variables in a multivariable model.** Which variables to include when the question asks for adjustment ("adjusted", "controlling for", "multivariable"). Their definitions are judged under covariates.
- **Left truncation (delayed entry).** BPC includes only patients whose tumors were sequenced, so time before sequencing is selectively observed. For a time-to-event outcome whose index date can precede the NGS report (any index date other than sequencing itself or a date defined to follow it), how patients enter the risk set: for example, left truncation at the NGS report date, restriction to patients sequenced before the index date, or an explicit choice to ignore delayed entry. This applies to every time-to-event question, genomic or not, and whether or not any analysed patient in this cohort was sequenced after the index date. When the index date itself is left open, it applies unless every reasonable reading of the index date is sequencing or a date defined to follow it. Using a canonical endpoint variable does not settle it. For a question that touches genomics, it is a separate gap from genomic testing timing under the denominator (see that concept).
- **Exposure ascertained after the index date.** When an exposure or biomarker status can be ascertained after the index date (for example, ever receiving a drug, or a biomarker from a test that can follow the index date), how that time is handled: a landmark, a time-varying exposure, an exposure defined before the index date, or left truncation at the testing date. A stated rule of left truncation at the testing date settles this for biomarker status from that test. For a biomarker exposure, an open choice here touches the covariates too (see "Where a gap belongs").

---

## Conventional defaults

When a question uses any of these terms without further qualification, assume the convention listed. A choice these defaults settle is not a gap.

- **"non-synonymous mutation"** → MAF `Variant_Classification` ∈
  {Missense_Mutation, Nonsense_Mutation, Frame_Shift_Del, Frame_Shift_Ins,
  Splice_Site, In_Frame_Del, In_Frame_Ins, Nonstop_Mutation,
  Translation_Start_Site}. Excludes Silent, intronic, UTR, RNA, IGR,
  Splice_Region.

- **"mutation in gene X"** → any non-synonymous (above) somatic variant in
  `data_mutations_extended.txt` with `Hugo_Symbol == X`. The same holds
  for "X-mutant" and "X-mutated", unless clinicians commonly read that
  gene's mutant label as a specific set of variants (for example, the
  activating or drug-sensitizing variants of a gene whose targeted
  therapies act only on those). Then, as with drug classes (see "Drug
  classes"), the question must list the variants that count, or say "any
  non-synonymous mutation".

- **Patient-level mutation aggregation** → patient is positive if ANY of
  their sequenced samples carries the qualifying variant (any-positive
  aggregation). Alterations described as co-occurring (for example, a
  "co-mutation") are each read this way, at the patient level; they need
  not be on the same sample.

- **Tested for gene X** → a patient or sample was tested for gene X only
  if its panel covers X: use `data_gene_matrix.txt` (or the panel column
  in `data_clinical_sample.txt` with the `data_gene_panel_*` files). This
  applies whether or not the question says "panel covers gene X". Patients
  not tested for a gene are excluded (not treated as wild-type) when
  eligibility or the exposure is a mutation in that gene; for eligibility
  this is the denominator "Missing values" rule.

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
  different payloads as distinct drugs. Whether a named drug covers its
  biosimilar or prodrug forms is not, by itself, a gap. The same substring rule applies to
  "regimen containing X and Y" (both drugs present, other drugs allowed).
  A combination named without "containing", such as "X plus Y" or "X with
  Y", leaves open whether regimens with additional drugs qualify. Naming an
  optional drug ("X plus Y, with or without Z") settles only Z; whether
  regimens that add any other drug qualify stays open. Only wording that
  excludes other drugs ("only", "exactly", "and no other drugs") or allows
  them ("containing", "with or without other drugs") settles it.
  "Single-agent", "monotherapy", and "as the only drug" mean exactly one
  drug. "Doublet" or "triplet" fixes how many drugs of the named kind the
  regimen has but leaves open whether drugs of another kind may be added.
  Excluding a drug or class ("without concurrent immunotherapy") settles
  only that drug or class, as naming an optional drug does. Drugs are
  concurrent, or combined, when they belong to the same BPC regimen (the
  same row of `regimen_cancer_level_dataset`). The substring rule does not
  settle clinical-trial regimens whose drug names are masked: a masked row
  may or may not contain X. A question that names drugs or a drug class
  must say how trial regimens are handled (for example, excluding them,
  or assuming they did not contain the named drugs).

- **Clinical-trial regimen** → a regimen with `drugs_ct_yn == 'Yes'`,
  whether its `regimen_drugs` string masks all, some, or none of its
  drugs. Institutions differ in which trial drugs they mask (see the
  Analytic Data Guide), so a masked drug name is not what makes a regimen
  a trial regimen.

- **Drug classes** → BPC records drugs by name and has no drug-class
  field. A drug family whose members clinicians agree on (for example,
  platinum, taxane, PD-1/PD-L1 inhibitor, or immune checkpoint inhibitor)
  means those members and is not a gap. A family whose membership
  clinicians commonly read differently must list its drugs: for example,
  "EGFR inhibitor" is often read as the EGFR tyrosine kinase inhibitors
  alone, though antibodies such as cetuximab and necitumumab also target
  EGFR. So must a broad term (for example, chemotherapy, immunotherapy, or
  targeted therapy). If such a term does not list its drugs or otherwise
  say which count, the component that uses it needs clarification. The Analytic Data Guide's mentions of drug kinds (for
  example, "anti-neoplastic drugs, immunotherapies, targeted therapies,
  and hormone therapies") describe what BPC curated; they do not define
  class membership.

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
  itself, a gap. Wording about time ("ever", "at any point") does not widen this; only wording that names other
  cancers (for example, "for any cancer") does. A regimen not linked to
  any cancer (`ca_seq` missing, which the Analytic Data Guide allows) is
  not attributed to the key index cancer and does not count. The same
  holds for line of therapy: lines are numbered only among regimens for
  the key index cancer (`regimen_number_within_cancer`, not
  `regimen_number`, which counts regimens for all cancers), so a regimen
  for another cancer, or one linked to no cancer, does not move a later
  regimen to a later line.

- **Patients with no regimen** → BPC curated cancer-directed regimens for
  every cancer diagnosis, so a patient with no regimen for the key index
  cancer received no cancer-directed drug for it. Count such patients as
  untreated, not as missing.

- **Catch-all drug entries** → "Other antineoplastic", "Other hormone",
  and "Other NOS" in the drug fields never match a named drug or a drug
  class. A patient whose only regimens use them did not receive any named
  drug or class.

- **Hazard ratio / Cox model** → a hazard ratio means an unadjusted
  (univariable) Cox proportional-hazards model from `lifelines`, unless the
  question names covariates, says "adjusted", or specifies another model
  structure. Wording about delayed entry or left truncation (for example,
  "risk-set adjustment for delayed entry") sets how patients enter the
  risk set; it does not make the model multivariable. The p-value for a
  hazard ratio is the Wald p-value for that coefficient in the fitted
  model's summary (`CoxPHFitter.summary['p']`).

- **"NGS testing date"** → the patient's earliest sequencing report date
  for the key index cancer (`dx_cpt_rep_days` in
  `cancer_panel_test_level_dataset.csv`). `dx_cpt_rep_days` counts days
  from diagnosis of the cancer; to place it on another time scale, subtract
  that origin's days from diagnosis (for regimen start,
  `dx_reg_start_int` in `regimen_cancer_level_dataset.csv`). When
  left-truncating, a patient enters the risk set at that date on the
  outcome's time scale, or at the time origin if sequencing came first. A
  patient whose entry time is on or after their event or censoring time is
  never at risk: drop that patient rather than shifting either time.

- **Outcomes of treated patients** → when every patient analysed has a
  qualifying regimen (one regimen group, regimens compared with each
  other, or groups compared among recipients of a regimen), time-to-event
  outcomes are measured from the start of that regimen unless the question
  says otherwise.

- **Median with 95% CI for time-to-event** → Kaplan–Meier median from
  lifelines `KaplanMeierFitter` (default `alpha=0.05`), with the CI from
  `median_survival_times(kmf.confidence_interval_)`: the Brookmeyer–Crowley
  method applied to lifelines' default log(-log) pointwise band.

- **Proportion with 95% CI** → Wilson score interval
  (`statsmodels` `proportion_confint(..., method="wilson")`).

- **Median with 95% CI for non-survival continuous variable (e.g., age)**
  → exact distribution-free CI (binomial order-statistic), or equivalently
  the lifelines/scipy default.

- **Stage at diagnosis** → `stage_dx`, BPC's stage at diagnosis, when a
  question restricts, groups, or measures by stage at diagnosis. A patient
  "diagnosed at" or "diagnosed with" a stage (for example, "diagnosed with
  Stage IV disease") has that stage at diagnosis. A stage term that does
  not say "at diagnosis", "diagnosed at" or "diagnosed with", "de novo", or
  "newly diagnosed" leaves the disease-burden concept open (see "Disease
  burden").

- **"Survival"** → overall survival, when the question does not name
  another endpoint.

- **"OS"** → use the canonical OS variable for the stated index date when relevant
  (`tt_os_g_*` from regimen start, `tt_os_dx_*` from diagnosis,
  `tt_os_adv_*` from advanced-disease diagnosis when the question defines
  advanced disease as BPC does (see "Disease burden"), etc.). If there is no
  canonical variable for the index date, derive OS from the same death and
  last-known-alive dates, censoring at last known alive.

- **Canonical endpoint variables** → the BPC OS and PFS variables carry
  their own event and censoring rules, set out in the Analytic Data Guide.
  Death is an event for OS and for every PFS variant. OS, and PFS from
  advanced-disease diagnosis (`*_adv_*`), censor at the last known alive
  date. PFS from regimen start (`*_g_*`) censors at the next
  cancer-directed regimen or at the end of follow-up: the start of the
  next regimen if there is one, otherwise the last known alive date. PFS
  variables are populated only for cancers that were stage IV at
  diagnosis or that later developed distant metastasis; for the latter,
  PFS from regimen start is defined only for regimens that began after the
  date of distant metastasis. A later distant metastasis
  (`dmets_stage_i_iii`) is recorded only for a patient's first BPC Project
  Cancer, so a later index cancer that was Stage I–III at diagnosis has no
  PFS. An analysed unit whose canonical variable is blank is outside that
  variable's population and is dropped. Using the canonical
  variable for the stated endpoint and index date settles censoring, but
  not how patients enter the risk set (see "Left truncation").

- **Key index cancer** → restrict analyses to index cancers in
  `cancer_level_dataset_index.csv` (`redcap_ca_index == 'Yes'`). Unless a
  question *explicitly* asks about multiple index cancers, derive a single
  **key index cancer** per patient = the **earliest-diagnosed genomically
  profiled index cancer that meets the question's cancer-level eligibility
  criteria** (for example stage at diagnosis, histology, a biomarker found
  on that cancer's specimens, or a qualifying regimen given for that
  cancer). A genomically profiled index cancer is one
  with an associated panel test in `cancer_panel_test_level_dataset.csv`
  (joined on `record_id` + `ca_seq`). A panel test that BPC lists under
  more than one cancer, because it could not be tied to a single
  diagnosis (see the Analytic Data Guide), belongs to each cancer it is
  listed under. "Earliest" orders cancers by `dob_ca_dx_days` where
  present, otherwise `ca_seq`. A later, separate primary that meets the
  criteria therefore makes the patient eligible even when an earlier index
  cancer does not, unless the question excludes patients with prior primary
  cancers. A cancer-level characteristic that is the outcome or a covariate,
  rather than an eligibility criterion, does not choose the key index
  cancer: it is measured on the key index cancer that the eligibility
  criteria choose. When the question has no cancer-level criteria, the key index
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
  covariates, says "adjusted", or specifies another model structure;
  delayed-entry wording such as "risk-set adjustment" does not count).
- Multi-sample patient aggregation (use any-positive).
- "Contains" drug match (use substring). This does not settle "X plus Y"
  wording or masked clinical-trial regimens (see "regimen containing drug
  X").
- Multiple index cancers per patient (use the key index cancer).
- Whether a regimen or genomic specimen "belongs to an index cancer"
  (resolved by the key-index-cancer convention).
- Biosimilar or prodrug forms of a named drug (treat biosimilars as the
  reference molecule; exclude distinct-INN ADCs).
- Unknown values of a variable used only for eligibility (criteria apply
  only to records known to meet them; see "Missing values" under the
  denominator).
- Anatomic / histologic cohort scope for patient-level demographic
  questions (they are answered at the patient level).
- Which canonical BPC variable to use for a stated endpoint and index date,
  or censoring for that variable (use the canonical variable). This does
  not cover left truncation, or which stage groups "advanced" covers in an
  index date such as "onset of advanced disease".

## Data that cannot support the question

When the construct a component needs has no clearly identified variable in the data dictionary or the Analytic Data Guide, or several plausible proxies there, that component needs clarification. Judge this from which variables exist, not from their values. A question whose components are all clear but whose estimand cannot be estimated from the data is not a clarity problem: judge every component clear and return the structured `unanswerable` result described under "Answer-type schemas".

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
  **`data_fusions.txt`**, **`data_sv.txt`** (where present) — TAB-DELIMITED, no `#` rows.

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
