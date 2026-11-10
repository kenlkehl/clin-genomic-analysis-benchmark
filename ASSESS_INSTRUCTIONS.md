# Instructions for the assess task of the clin-genomic-analysis-benchmark

You are being evaluated on the Clinical Genomic Analysis Benchmark, which measures performance on clinico-genomic data analyses in oncology. In this task you judge whether each of the four components of a research question — the cohort denominator, the outcome, the independent variables and covariates, and the statistical analysis plan — states what the researcher means precisely enough to yield one analysis. The questions are written for data from the AACR GENIE Biopharma Collaborative (BPC), which includes longitudinal, curated clinical annotations of cancer diagnosis, treatment, and outcomes, linked to tumor next-generation sequencing (NGS) panel results.

The task tests whether you can tell when a researcher's words leave their intent clear. Every dataset defines its own terms (stage, a treatment regimen, progression), and a researcher's words may or may not match those definitions. For example, a researcher who writes "patients with Stage IV disease" may mean only patients who were Stage IV when first diagnosed, as a dataset that records stage only at diagnosis would define them, or may also mean patients whose earlier-stage cancer later spread. A dataset's definition does not tell you which the researcher meant. So, clarity is judged from the question and this document alone, not from the data or their documentation.

The conventions that follow this document are part of these instructions; they fix the meaning of common terms. The question to assess comes last.

---

## How to score highly (quick checklist)

1. **Judge each component separately.** For the denominator, the outcome, the covariates, and the analysis plan, ask: could two careful researchers who read this question with this document mean different things, and so build this component differently? If so, it needs clarification.
2. **Settle choices only from the question and this document**: the question's wording, a convention, or a named definition the question adopts (see "Named definitions").
3. **Work through the catalog for each component.** Each component below catalogs the choices that questions leave open. If a question leaves open a choice the catalog does not list, it still needs clarification unless the wording, a convention, or a named definition settles it.
4. **Apply the conventions first.** A choice a convention settles is not a gap. Never flag anything listed under "Avoid flagging on these grounds alone".
5. **Flag a gap in every component it touches**: every component whose part of the analysis cannot be built until the gap is resolved (see "Where a gap belongs").
6. **Return only the JSON object** described under "Output".

Each component is scored on its own with balanced accuracy, so flagging a component that is clear costs as much as missing one that is not.

---

## The cohorts

Each cohort holds patients with one key cancer type, named by the cohort, whose tumors were sequenced with an NGS panel, together with curated data on their cancer diagnoses, cancer-directed drug regimens, and outcomes. Patients enter a cohort because a tumor was sequenced with a next-generation sequencing genomic panel, and sequencing can happen at any point in the disease course: before, during, or after the diagnosis, treatment, or event a question asks about. A patient may have more than one cancer, including more than one cancer of the cohort's type, and more than one sequenced specimen, either across more than one primary cancer or within a single primary cancer trajectory. Some patients may have had unrelated primary cancers in addition to the key genotyped cancer type that conferred eligibility for the cohort. Some patients were treated on clinical trials.

---

## The task

You receive the question and its cohort. For each of the four components, decide whether the question needs clarification before one analysis follows from it. The question is ambiguous if any component needs clarification.

### Output

All four keys are required:

```json
{ "denominator":   { "needs_clarification": false, "rationale": "<one or two sentences>" },
  "outcome":       { "needs_clarification": true,  "rationale": "<one or two sentences>" },
  "covariates":    { "needs_clarification": false, "rationale": "<one or two sentences>" },
  "analysis_plan": { "needs_clarification": false, "rationale": "<one or two sentences>" } }
```

A component the question does not involve (for example, covariates in a single-group count question) does not need clarification. Each `rationale` says what is unclear, or how the question or a convention specifies the component. Rationales are recorded but not scored.

Reply with only this JSON object.

---

## Judging clarity

The standard for every component is the same: does the question, read with this document, fix every choice a careful analyst would have to make in building this part of the analysis? Imagine writing the analysis protocol before analyzing the dataset it will run on. Every choice the protocol has to make must be fixed by the question's wording, by a convention in this document, or by a named definition the question adopts. A component can be clear even if building it takes several steps.

### Named definitions

A question can settle a choice by adopting a named definition:

- a published standard or criterion, by name (for example, "AJCC 8th edition stage" or "response by RECIST 1.1");
- an endpoint the conventions define (see "Named endpoints"); or
- the data source's own definition, by explicit reference (for example, "advanced disease as the data source defines it", or a named data variable and its values).

Adopting a definition settles everything that definition covers, even if you do not know its details. It settles nothing else: for example, adopting a named endpoint does not settle how patients enter the risk set (see "Left truncation (delayed entry)").

### What counts as a gap

Each component below catalogs the choices a question has to fix for that component to be clear. If a question leaves open a choice the catalog does not list, it still needs clarification unless the wording, a convention, or a named definition settles it. A catalogued choice that cannot arise in the question (for example, left truncation when there is no time-to-event outcome) is not a gap.

If the question leaves a choice open and no convention settles it, the component needs clarification, even if every reading would give the same result.

**Unknown values.** Treat every characteristic as one that can be unknown for some patients, except those that cancer registries record for every patient: date of birth (and so age at any date), sex, the date each cancer was diagnosed, and vital status with the date of death or last known contact. A question that groups or measures by any other characteristic must say how unknown values are handled, unless a convention does (see "Missing values" under each component). A value recorded too coarsely for the question's grouping is unknown for that grouping: for example, a stage recorded only as "Stage I–III" when the question groups Stage III apart from Stages I–II.

### Where a gap belongs

Flag a gap in every component it touches: every component whose part of the analysis cannot be built until the gap is resolved. A construct touches:

- the **denominator** if it decides who or what is analysed;
- the **outcome** if it decides what is measured or counted, or when measurement starts or ends (for example, the index date of a time-to-event outcome);
- the **covariates** if it defines an exposure, comparison group, stratifier, or adjustment variable;
- the **analysis plan** if it decides how the estimate is computed.

The same term can sit in different components in different questions, and in more than one component of the same question. For example, the term "metastatic" restricts the denominator in "among patients with metastatic disease, …", is the outcome in "what proportion had metastatic disease", and is a covariate in "compare survival between patients with and without metastatic disease". The rules for a construct apply wherever it appears.

A gap that touches several components is flagged in each of them. For example:

- In "overall survival from the start of first-line therapy among patients who received first-line X", an unclear "first-line" decides both who is analysed and when each patient's clock starts: a patient who received X after surgery and again at relapse is analysed under either reading, but from a different start date. So the denominator and the outcome both need clarification.
- A gap that decides only whether a patient is analysed, and not when an analysed patient's clock starts, touches the denominator alone. For example, when a line term is tied to a disease state ("first-line X for metastatic disease", outcome measured from the start of that regimen), the readings differ in who is included, but every reading starts an included patient's clock at the same regimen of X, so the outcome is clear. Flag the outcome only if some patient analysed under two readings would get different start dates.
- When the compared groups do not cover everyone in the stated population (for example, "drug A versus drug B" among all treated patients, where patients who received neither drop out), an unclear group definition decides both which group a patient is in and who is analysed, so the covariates and the denominator both need clarification.
- An unclear definition that changes only which group a patient is in, and not who is analysed, touches only the covariates. If it also decides who counts as tested (for example, whether copy-number changes count, which limits the analysis to patients with copy-number results), it touches the denominator too.
- An unstated index date touches the denominator as well as the outcome when some reasonable readings exist only for some patients (for example, onset of advanced disease, or the start of a regimen in a cohort not defined by treatment).
- When a biomarker exposure can be ascertained after the index date and the question gives no timing rule (see "Exposure ascertained after the index date"), the choice can move a patient between groups: for example, a later test may show a mutation that an earlier test did not. So the covariates need clarification as well as the analysis plan. A stated rule that settles that concept settles the covariates too.
- When a question leaves open how unknown values of the outcome or of a covariate (an exposure, comparison group, stratifier, or adjustment variable) are handled, it also leaves open whether those units are analysed. So the denominator needs clarification too, along with the component whose variable is unknown.

---

## The four components

For each concept, the catalog says what must be fixed, and which wording fixes it or leaves it open. Conventions it refers to are under "Conventions".

### 1. Cohort denominator

Which patients, or which cancers, regimens, or specimens, are analysed.

**The cancer**

- **The cohort's cancer.** A reference to the cohort's own cancer, by name or usual abbreviation (for example, "patients with colorectal cancer" in the colorectal cancer cohort), means each patient's key index cancer and is clear (see "Key index cancer"). It is not a restriction by histology or site.
- **Histology.** A restriction to a histologic type (for example, "adenocarcinoma") leaves open whether tumors of mixed histology that include that type, and carcinomas not otherwise specified, count, unless the question says, or adopts a named classification (for example, a WHO classification edition, or the data source's curated histology categories).
- **Molecular and receptor-defined subtypes.** Receptor or subtype status means the result as the testing laboratory reported it (positive, negative, or equivocal). A restriction by such a status must say how equivocal or borderline results count and, because status can be measured more than once and can change, which measurement counts (for example, at diagnosis, or the most recent before the index date).
- **Primary site.** A restriction or grouping by primary cancer site must name the sites that count. A grouping whose boundaries are drawn differently in practice must say where they fall.
- **Other cancers.** Patients who also had other primary cancers are analysed unless the question excludes them (see "Other primary cancers"). An exclusion must say which other cancers count (any, or invasive cancers only) and in which period (before the key index cancer, or at any time).

**Stage and disease burden**

- **Timing.** Whether a stage or disease-burden term ("Stage IV", "Stage I–III", "early-stage", "localized", "locally advanced", "advanced", "metastatic") means the burden at initial diagnosis, or also covers burden reached later (for example, a distant recurrence after an earlier-stage diagnosis). Only wording that explicitly ties the stage to the cancer's initial diagnosis fixes it to diagnosis: "at diagnosis", "at initial diagnosis", "diagnosed at" a stage, "de novo", "newly diagnosed", or similar. Wording that names the later state fixes the other reading (for example, "Stage IV at diagnosis or distant metastasis after a Stage I–III diagnosis"). Any other reference to stage leaves it open. That includes a bare "Stage IV", "diagnosed with Stage IV" or "diagnosed with metastatic disease", and a phrase that dates a diagnosis of the stage, such as "after Stage IV diagnosis" or "from the date of metastatic diagnosis", because a distant recurrence is also diagnosed.
- **Stage groups.** Which stage groups a term covers, when clinicians use it for more than one set: "advanced" (Stage IV only, or Stages III–IV), "early-stage" (Stages I–II, or Stages I–III), "locally advanced" (which Stage III substages, and whether any Stage II counts). These terms fix no stage groups in any use: as a stage at diagnosis ("diagnosed at an advanced stage"), as a population ("patients with advanced disease"), or as a date ("overall survival from onset of advanced disease", "the first regimen for advanced disease"). "Metastatic" means distant metastasis (see "Metastatic"); it is open only on timing.
- **Recurrence.** For "recurrent" or "relapsed" disease, which recurrences count: local, regional, or distant.
- **Sites of metastasis.** For a restriction by a site of metastasis (for example, "patients with liver metastases"), when the site is assessed: at diagnosis, before the index date, or at any time.
- **Staging detail.** Stage at diagnosis follows the convention ("Stage at diagnosis"), so a question need not name a staging system, an edition, or clinical versus pathologic stage. A question that names one (for example, "pathologic stage") restricts to it.
- **Unknown or coarse stage.** For eligibility, settled by the "Missing values" rule below. For a stage grouping that is an outcome or covariate, the question must say how unknown and coarse values count.

**Biomarker-defined eligibility**

For a population defined by the presence or absence of a biomarker:

- **Genes.** The gene or gene set; for a set, whether an alteration in any one gene qualifies, or one in each.
- **Alteration types.** Which types count: mutations (single-nucleotide variants and small insertions or deletions), copy-number changes, fusions or rearrangements, or other structural variants. "Mutation", "mutant", and "mutated" mean mutations only (see "Mutation in gene X"). "Alteration", "altered", and "aberration" leave the types open.
- **Variants.** Whether only some variants count (named variants or codons, hotspots, or variants a named knowledge base classifies as oncogenic or pathogenic), or any non-synonymous mutation (the default; see "Mutation in gene X" for "X-mutant" labels clinicians read narrowly), and any variant allele fraction threshold.
- **Copy-number levels.** For copy-number changes, which levels count: for example, high-level amplification only or any gain, and deep (homozygous) deletion only or any loss.
- **Fusions.** For fusions or rearrangements, which partner genes count.
- **Germline variants.** "Mutation" means a variant reported by tumor sequencing (see "Mutation in gene X"). A question about inherited (germline) variants must say how they are identified.
- **Composite biomarkers.** For tumor mutational burden, microsatellite instability, homologous recombination deficiency, and similar measures: the measure, the assay (for example, panel sequencing or immunohistochemistry), and the threshold.
- **Biomarkers not from sequencing.** For immunohistochemistry, in situ hybridization, or other assays: the assay, the scoring method, the threshold, and how equivocal results count.
- **Specimen.** Which specimen counts when a patient has several is settled by any-positive aggregation (see "Patient-level aggregation"), unless the question names one (for example, the first specimen, or metastatic specimens only).
- **Tested patients.** Settled by convention: a patient is tested for a gene only if the assay covered it, and untested patients are excluded, not treated as negative (see "Tested for gene X").

**Genomic testing timing.** For a question that touches genomics (a biomarker in the eligibility criteria, an exposure, or the outcome), what to do about patients whose index genomic test (the first sequencing report for the key index cancer) had not yet happened at a time point that defines eligibility. Such time points include the start of a qualifying regimen, the date of advanced or metastatic disease, the diagnosis date when patients are followed from diagnosis, and any landmark. Acceptable rules include requiring testing before that time point, including patients whatever the testing date, or a stated delayed-entry rule. A stated delayed-entry rule for the outcome's index date (for example, left truncation at the sequencing report date) settles this for that time point. The concept applies whenever such a time point is not defined to follow testing. When the time point is itself left open (for example, an unstated index date), the concept applies unless every reasonable reading of it is defined to follow testing. When the time point is also the index date of a time-to-event outcome, this concept and left truncation under the analysis plan are separate gaps: if the question states neither, flag both components. Requiring testing before the index date, or a stated delayed-entry rule, settles both. Including patients whatever the testing date settles eligibility but leaves risk-set entry open.

**Treatment-defined eligibility**

- **Qualifying drugs.** The drugs that qualify. A named drug is clear (see "Drugs"). A drug class is clear only if clinicians agree on its members (see "Drug classes"). Combination wording ("X plus Y", "containing X and Y", "doublet", "monotherapy") follows "Regimens and combinations".
- **Clinical-trial treatment.** Drugs given on a clinical trial can be blinded or unrecorded, so a regimen given on a trial may or may not contain a named drug. A question that selects, groups, or measures by named drugs or classes must say how trial regimens count: for example, excluding them, or assuming they did not contain the named drugs.
- **Line of therapy.** For terms such as "first-line", "second-line", "later-line", "previously treated", or "treatment-naive":
  - the disease state the line refers to, for example the first regimen for the cancer in any setting, versus the first regimen given for advanced or metastatic disease; and
  - when the line refers to advanced disease that is not exclusively de novo, how treatment given earlier in the early-stage setting (neoadjuvant, adjuvant, or other curative-intent therapy) counts. Some line definitions settle this: "the first regimen started on or after the date of advanced disease" does not count earlier regimens (which stage groups "advanced" covers is a separate question; see "Stage groups"), and "the first regimen for the cancer in any setting" counts them all. A bare "first-line for metastatic disease" that includes recurrent disease does not settle it. Nor does a line term without a setting, such as "first-line systemic therapy for the cancer": "first-line" is often read as the first treatment for advanced disease, and "for the cancer" can be read as any setting, so the wording leaves open whether early-stage treatment counts.
  - A line term qualified by a stage (for example, "first-line therapy for Stage IV disease") also raises the stage and disease-burden concepts above; both belong to the denominator, and to the outcome only when they move an analysed patient's start date (see "Where a gap belongs").
- **Setting and intent.** For a treatment tied to a setting ("adjuvant", "neoadjuvant", "perioperative", "curative-intent", "palliative"), how the setting is identified: by timing relative to surgery, diagnosis, or recurrence (for example, "started after surgery and before any recurrence"), or by adopting a recorded treatment intent. A treatment "for" a disease state that some analysed patients reached after diagnosis (for example, "the first regimen for metastatic disease" when recurrent disease counts) needs a timing rule, such as "started on or after the date of distant metastasis".
- **Prior treatment and progression on treatment.** For "previously treated", "after progression on X", or "refractory to X": which earlier treatment counts (any cancer-directed regimen, or a named drug or class), in which setting, and how progression on that treatment is established.
- **Which regimen.** When a patient can have more than one qualifying regimen (for example, a patient who received drug X twice), which counts: the first, each as its own unit, or another stated rule. A line of therapy fixes it ("first-line X"); "patients who received X", with an outcome measured from X, does not.
- **Receipt.** A patient received a drug if they started it (see "Receipt"). A minimum duration, dose, or number of cycles applies only if the question states it.
- **Surgery, radiation, and other procedures.** The procedure that counts (for example, resection versus biopsy; any radiation versus radiation to a named site or with curative intent), and its timing relative to diagnosis and to other treatment.

**Patient characteristics**

- **Age.** The time point at which age is measured (at diagnosis, at sequencing, or at the index date). A descriptive term ("elderly", "older", "young") needs a cutoff.
- **Race and ethnicity.** The categories, whether ethnicity is part of the grouping or a separate characteristic, and how patients recorded with more than one race count.
- **Clinical characteristics.** For performance status, comorbidity, body mass index, and similar characteristics: the scale or definition, the cutoffs, and when the characteristic is measured.
- **Cutoffs.** Read literally (see "Cutoffs and ranges"). A descriptive term with no cutoff ("high", "long-term", "rapid") leaves it open.

**Time windows**

- **Calendar period.** The diagnosis or treatment dates that qualify. Ranges include both ends (see "Cutoffs and ranges").
- **Follow-up requirements.** Any minimum follow-up, or requirement to be alive at a time point.
- **Windows around an event.** For "within N months of diagnosis" and similar wording, the reference date and the window. Months follow the unit convention (see "Time units").

**Implied restrictions.** Wording that presupposes something not every patient in the stated population has (for example, "during their treatment course" when some patients received no treatment) leaves open whether patients without it are analysed.

**Unit of analysis.** The unit analysed (patient, cancer, regimen, or specimen), and which observations qualify when a patient can contribute several regimens, specimens, or cancers.

**Missing values.** Eligibility criteria apply only to units known to meet them (see "Unknown values in eligibility criteria"). Unknown values in a characteristic used only for eligibility are therefore not, by themselves, a gap. Unknown values of the outcome or of a covariate are different: when their handling is open, the denominator needs clarification too (see "Where a gap belongs").

### 2. Outcome

What is measured or counted for each analysed unit. The stage, treatment, and biomarker concepts under the denominator apply here too when the construct is part of the outcome (for example, "what proportion had metastatic disease").

- **Endpoint.** The endpoint itself, when the wording admits more than one (for example, a "benefit", "outcome", or "did better" that could be survival, response, or time to next treatment). "Survival" means overall survival (see "Survival").
- **Index date.** The time zero of a time-to-event outcome: diagnosis of the key index cancer, onset of advanced or metastatic disease (which needs its stage groups and timing defined; see "Stage and disease burden"), the start of a regimen (fixed by "Outcomes of treated patients" when every analysed patient has a qualifying regimen), surgery, sequencing (see "NGS testing date"), or a landmark.
- **Overall survival.** Settled by convention (see "Overall survival").
- **Progression-free survival and other progression endpoints.** Settled by a named endpoint (see "Named endpoints"). Otherwise the question must fix:
  - how progression is ascertained: from imaging reports, from treating-clinician assessment, from either, from both, or by formal criteria such as RECIST 1.1. An unqualified "PFS" or "progression" leaves it open;
  - for "time to progression", how death without progression is handled ("progression-free survival" counts death as an event; see "Progression-free survival");
  - the censoring date (for example, the last known alive date, or the last disease assessment), and, when follow-up starts at a regimen, whether starting the next regimen censors it.
- **Recurrence and event-free endpoints.** For recurrence-free, disease-free, event-free, or distant-metastasis-free survival, or time to recurrence: which events count (local, regional, or distant recurrence; a new primary cancer; death), how recurrence is ascertained, and the censoring date.
- **Treatment-based endpoints.** For time to next treatment, time to treatment discontinuation, or duration of therapy: what ends the interval (the start of the next regimen, discontinuation, death), what counts as a next treatment (any new regimen, or one that adds a new drug), the censoring date, and whether death is an event, a censoring event, or a competing event.
- **Competing events.** For an outcome that another event can preclude (for example, time to next treatment when death can come first), whether that event counts as an event, censors follow-up, or is a competing event (estimated by cumulative incidence rather than Kaplan–Meier).
- **Response.** The criteria (formal criteria such as RECIST 1.1, or the assessment in imaging reports or clinician notes), which response counts (best overall response, or the response at a stated assessment), whether a response must be confirmed, which categories count as a response (complete and partial response; whether stable disease counts, as for disease control), and how patients without an assessment count.
- **Outcomes at a time point.** For "2-year survival", "alive at 12 months", and similar outcomes: the time point and its origin. A survival probability or rate is a Kaplan–Meier estimate (see "Survival at a time point"). A crude proportion ("what proportion died within 2 years") must say how patients followed for less than that time count.
- **Characteristics measured.** For a characteristic that is measured or counted (a stage, a site of metastasis, a test result): when it is measured (at diagnosis, before the index date, within a window, or ever during follow-up), and the categories reported.
- **Treatment received.** When the outcome is receipt of a treatment: the drugs or classes that count (see "Drug classes"), how trial regimens count, the window in which treatment counts, and the setting. Treatment for other cancers does not count (see "Treatment attribution").
- **Biomarker outcomes.** For the prevalence of a biomarker: the biomarker-definition concepts under the denominator. The denominator of a prevalence is the patients or specimens tested for it (see "Tested for gene X").
- **Counts.** What is counted (patients, cancers, specimens, or regimens), and whether a unit is counted once or each time it occurs.
- **Missing values.** How analysed units with an unknown outcome value are handled: for example, whether patients with unknown status are dropped from a proportion's denominator or counted as not having the outcome. An open choice here touches the denominator too (see "Where a gap belongs"). Overall survival and the named endpoints settle this through their own definitions.

### 3. Independent variables and covariates

Every exposure, comparison group, stratifier, and adjustment covariate must be defined precisely enough that each analysed unit falls in exactly one category, or takes exactly one value. The stage, treatment, biomarker, and genomic-testing concepts under the denominator apply here too when the construct is a covariate.

- **Biomarker exposures.** The biomarker-definition concepts under the denominator (genes, alteration types, variants, copy-number levels, fusion partners, and the assay and threshold for other biomarkers), and which tests count when one can follow the index date (see "Exposure ascertained after the index date"). Which specimen counts, and untested patients, are settled by convention.
- **Treatment exposures.** The drugs or classes that count, combination wording, how trial regimens count, the setting, and the window in which treatment counts (before the index date, at it, or ever).
- **Clinical and demographic covariates.** The characteristic and its definition, when it is measured (for example, age at diagnosis, at sequencing, or at regimen start), and its categories or cutoffs (see "Patient characteristics" under the denominator).
- **Comparison groups.**
  - The groups compared, and the reference group for a ratio or difference (see "Reference group").
  - Whether units that fit neither group are analysed (see "Where a gap belongs"), and how a unit that fits both is assigned (for example, a patient who received both drugs, in different lines).
  - In "with versus without X", the group without X is the units tested for X with no qualifying result (for a biomarker), or the units not given X in the window the question states (for a treatment).
  - Whether there is a comparison at all, when the stated population makes the exposure constant.
- **Stratifiers and subgroups.** Each subgroup's definition, and whether estimates are reported within subgroups or as an interaction (see "Statistical estimand").
- **Continuous variables.** Whether a continuous variable is used as continuous or in categories, and, when its own estimate is reported, its unit (for example, per year or per 10 years of age).
- **Measurement time.** When a characteristic is measured (at diagnosis, before the index date, within a window, or ever during follow-up).
- **Missing values.** How units with an unknown value of an exposure, comparison group, stratifier, or covariate are handled: for example, a separate category, exclusion, or another stated rule. An open choice here touches the denominator too (see "Where a gap belongs").

### 4. Statistical analysis plan

How the estimate is computed from the denominator, outcome, and covariates.

- **Statistical estimand.** The quantity to report, such as a hazard ratio, a median, a difference in medians, a survival probability at a stated time, a proportion, an odds ratio, a p-value, or subgroup-specific estimates versus an interaction. "Compare survival between A and B" and "is there an association between X and survival" leave the estimand open.
- **Model or test.** Which model or test produces the estimand when more than one could: for example, Cox proportional hazards, a stratified Cox model, a time-varying exposure, a landmark analysis, logistic regression, a log-rank test, a chi-square test (with or without continuity correction) versus Fisher's exact test, or a t-test versus a Wilcoxon rank-sum test. The conventions fix some of these, including the unadjusted Cox model for a hazard ratio and the Kaplan–Meier median.
- **Variables in a multivariable model.** Which variables to include when the question asks for adjustment ("adjusted", "controlling for", "multivariable"). Their definitions are judged under covariates; their functional form follows "Model terms".
- **Left truncation (delayed entry).** Patients enter these cohorts only after a tumor is sequenced (see "The cohorts"), so time before sequencing is selectively observed. For a time-to-event outcome whose index date can precede the sequencing report (any index date other than sequencing itself or a date defined to follow it), how patients enter the risk set: for example, left truncation at the sequencing report date, restriction to patients sequenced before the index date, or an explicit choice to ignore delayed entry. This applies to every time-to-event question, genomic or not. When the index date itself is left open, it applies unless every reasonable reading of the index date is sequencing or a date defined to follow it. Using a named endpoint or overall survival does not settle it. For a question that touches genomics, it is a separate gap from genomic testing timing under the denominator (see that concept).
- **Exposure ascertained after the index date.** When an exposure or biomarker status can be ascertained after the index date (for example, ever receiving a drug, or a biomarker from a test that can follow the index date), how that time is handled: a landmark, a time-varying exposure, an exposure defined before the index date, or left truncation at the testing date. A stated rule of left truncation at the testing date settles this for biomarker status from that test. For a biomarker exposure, an open choice here touches the covariates too (see "Where a gap belongs").
- **Landmark analyses.** The landmark time and the date it is measured from. Who is analysed, and how exposure is fixed, follow "Landmarks".
- **Subgroups and interactions.** Whether a difference between subgroups is reported as separate estimates or tested as an interaction.

---

## Avoid flagging on these grounds alone

- Choice of CI method for a median, proportion, or survival probability (use the convention).
- Which mutation types count as non-synonymous (use the convention).
- Unadjusted vs. adjusted Cox (use unadjusted unless the question names covariates, says "adjusted", or specifies another model structure; delayed-entry wording such as "risk-set adjustment" does not count).
- Multi-specimen patient aggregation (use any-positive).
- "Containing" drug match (any regimen that includes the drug). This does not settle "X plus Y" wording or clinical-trial regimens (see "Regimens and combinations").
- Several index cancers per patient (use the key index cancer).
- Whether a regimen or genomic specimen belongs to an index cancer (settled by the key-index-cancer convention).
- Biosimilar or prodrug forms of a named drug.
- Unknown values of a characteristic used only for eligibility (criteria apply only to units known to meet them; see "Missing values" under the denominator).
- Histologic or site scope for patient-level demographic questions (they are answered at the patient level).
- Staging system, edition, or clinical versus pathologic stage for stage at diagnosis (use the convention).
- Events and censoring of overall survival or of a named endpoint (use the definition). This does not cover left truncation, or which stage groups "advanced" covers in an index date such as "onset of advanced disease".
- Time units, cutoff boundaries, and the ends of a date range (use the conventions).
- Which data variable would record a concept, or whether a dataset records it at all. These are matters for the analysis, not for clarity.
