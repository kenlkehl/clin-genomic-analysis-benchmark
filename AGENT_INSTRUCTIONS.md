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
   delayed cohort entry or immortal time bias, missing time origin or filter cut-off). Then list 3–6
   short concept noun-phrases that together close every reasonable gap.
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

Only invoked if the agent classified the question as AMBIGUOUS. List the
concrete concepts a questioner would need to specify to make the question
deterministically answerable from the cohort data. Each concept should be
a short noun-phrase (≤ ~12 words), not a sentence. Aim for 3–6 concepts
that together close every reasonable interpretive gap.

**Output:**

```json
{ "concepts": ["<concept 1>", "<concept 2>", "..."] }
```

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

A question is AMBIGUOUS when it leaves at least one of these truly
underspecified, either upfront, or based on the actual cohort data.

- **Population definition** that isn't resolved by the conventional
  defaults above (e.g., "advanced cancer" with no anchor to a specific
  staging variable AND the cohort has both de novo Stage IV and
  metastatic-recurrent patients in roughly equal numbers, so the choice
  materially shifts the cohort).

- **Anatomic / histologic scope** for a *cancer-level* question against a
  cohort that contains materially different histologies or anatomic
  sub-types, when the question does not specify which to include and the
  choice would shift the analysis sample. (Patient-level demographic
  questions are exempt — see "Patient-level attributes" above.)

- **Outcome metric** with multiple plausible operationalizations the
  conventions don't pin down (e.g., "treatment response" without
  specifying RECIST / imaging / clinical, or "PFS" when multiple distinct
  PFS variables exist and the time-origin isn't stated). For example, in this dataset, PFS-I means PFS where progression events are defined based on imaging assessment; PFS-M means progression is defined based on oncologist assessment; PFS-I-or-M defines progression as the earlier of imaging or oncologist assessment that progression has occurred; and PFS-I-and-M defines progression as requiring that both imaging and oncologist assessments have ascertained progression.

- **Comparator group** not specified or not derivable (e.g., asking for
  a hazard ratio, but the population restriction makes the exposure constant within
  the analysis sample, so no comparator exists).

- **Drug-class boundaries** not deterministic from the cohort data (e.g.,
  "platinum-based chemotherapy" when the question hinges on whether
  intravesical platinum or neoadjuvant-only platinum counts, AND the
  dataset doesn't contain a standard flag).

- **Definition of "chemotherapy"** when used without qualification: it may
  mean cytotoxic chemotherapy only or any systemic therapy, and whether a
  cytotoxic-payload antibody-drug conjugate counts is unspecified. (A
  specific term such as "platinum-based chemotherapy" is not ambiguous on
  this ground.)

- **"First-line" / "second-line" / line-of-therapy** without an analytic
  anchor. There is no universal convention for "first-line" in oncology —
  the operational definition is bespoke to the clinical context (first
  regimen for the index cancer, first regimen after metastatic diagnosis,
  first systemic therapy, first cytotoxic regimen, first regimen after a
  specific landmark, etc.) and to the research question. Treat any
  unqualified line-of-therapy term as ambiguous unless the question pins
  down the population/setting AND the dataset has a corresponding pre-
  built flag.

- **Biomarker definition** beyond the conventions above. Receptor-status
  thresholds in BPC are not conventionally fixed (no canonical IHC cut-
  points or rules for equivocal cases).

- **Biomarker-frequency denominator** when a question asks what
  fraction/proportion of patients or samples have an alteration in gene X
  but does not say whether the denominator is *all* sequenced patients/
  samples or only those whose panel **covers gene X**. The two denominators
  differ whenever some panels omit the gene, so silence on this is a
  genuine ambiguity. (A plain count of patients/samples with the alteration
  — no denominator — is not ambiguous on this ground.)

- **Time origin** not specified when multiple are plausible AND the
  dataset offers different time-origin variables (e.g., a survival
  endpoint without saying from diagnosis vs. from regimen start vs. from
  advanced-disease index).

- **Filter cut-off** not specified (e.g., "long-term survivors" without a
  year threshold).

- **Delayed cohort entry** . BPC cohorts require that genomic data have been collected on a tumor specimen. This means that any question that may address events or index dates that could precede the genomic specimen must address the delayed cohort entry problem: Time before genomic testing is essentially 'immortal,' since patients who died without testing would not be in the cohort. For time to event analyses indexed before genomic testing, this can be addressed via risk set adjustment / adjustment for left truncation, although this does not eliminate the related challenge of bias due to genomic testing being done at moments of disease progression. To be unambiguous, a question vulnerable to this issue must describe how it will deal with it, even if there is no obvious best approach to dealing with it.

- **Immortal time bias** for an exposure defined by *receipt* of a treatment that can only occur after some time has elapsed (e.g., "received adjuvant/neoadjuvant therapy" vs not, or an "ever received treatment X" comparison anchored at diagnosis). Comparing such groups from a common origin without a landmark gives the treated group guaranteed event-free time before exposure, biasing the result. To be unambiguous, the question must define a landmark (e.g., restrict to patients alive and eligible at a fixed time after diagnosis and classify exposure as of that landmark); the conventional analysis is a landmark analysis.


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

| `answer_type`              | Required `answer` fields |
|---|---|
| `count`                    | `value` (int), `n` (int) |
| `proportion`               | `value` (float), `numerator` (int), `denominator` (int), `ci_low` (float), `ci_high` (float) — Wilson CI by default |
| `median_with_ci`           | `value` (float), `ci_low` (float), `ci_high` (float), `n_total` (int), `n_events` (int) |
| `hazard_ratio_with_ci`     | `value` (float), `ci_low` (float), `ci_high` (float), `p_value` (float), `n_total` (int), `n_events` (int) — Cox PH from lifelines |
| `odds_ratio_with_ci`       | `value` (float), `ci_low` (float), `ci_high` (float), `p_value` (float), `n_total` (int) |
| `pvalue`                   | `value` (float), `test_name` (str), `n_total` (int) |
| `categorical`              | `value` (str), `n_total` (int) |

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
