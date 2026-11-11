# Instructions for the analyze task of the clin-genomic-analysis-benchmark

You are an LLM agent being evaluated on the Clinical Genomic Analysis Benchmark, which measures performance on clinico-genomic data analyses in oncology. In this task you compute the answer to a research question from data derived from the AACR GENIE Biopharma Collaborative (BPC), which includes longitudinal, curated clinical annotations of cancer diagnosis, treatment, and outcomes, linked to tumor next-generation sequencing (NGS) panel results.

Every question you receive has been reviewed and is fully specified: each choice the analysis needs is fixed by the question's wording, by a convention, or by a named definition the question adopts (a published standard, a named endpoint, or the data source's own definition). The conventions that follow this document are part of these instructions.

This document describes what the task expects and how to map a question onto the BPC data. Outputs are JSON objects with the fixed schemas below; the harness rejects output that does not match them.

---

## How to score highly (quick checklist)

1. **Implement the question as written.** Follow every rule the question states and every convention that applies. Do not narrow or widen the question to fit what BPC records.
2. **Map each concept to BPC data fields** as "Implementing a clear question in BPC" describes.
3. **Compute deterministically** with the conventional methods (see "Statistics" in the conventions).
4. **Report a question BPC cannot support as unanswerable** (see "Answer-type schemas").
5. **Return only the typed JSON** (see "Answer-type schemas" and "Output discipline").

---

## The cohorts

Each cohort contains patients with a certain cancer type, named by the cohort, whose tumors were sequenced with an NGS panel, together with curated data on their cancer diagnoses, cancer-directed drug regimens, and outcomes. Patients enter a cohort because a tumor was sequenced, and sequencing can happen at any point in the disease course: before, during, or after the diagnosis, treatment, or event a question asks about. A patient may have more than one cancer, including more than one cancer of the cohort's type, and more than one sequenced specimen, which may be from the cancer of the cohort's type or from a different cancer. 

---

## The task

You receive the question text, the cohort name, the cohort directory (read-only), the data dictionary, and a scratch directory for intermediate files. The cohort directory also holds the BPC Analytic Data Guide, a PDF whose file name contains "Analytic Data Guide"; it defines BPC's derived variables, including the definitions and censoring rules of its endpoints. Implement the question as written, following "Implementing a clear question in BPC". Use Python, run through the shell (pandas, numpy, scipy, statsmodels, lifelines as needed), and write any intermediate files into the scratch directory. Do not search for an answer key; such attempts are monitored and will result in a zero score.

**Output:** see "Answer-type schemas" below.

---

## Implementing a clear question in BPC

Implement the question as written: do not narrow or widen it to fit what BPC records. The conventions are stated without reference to any dataset; map each concept to BPC data fields, as below. For a concept not listed, use the analytic-dataset variable that records the concept most directly for the key index cancer at the stated time, and name it in `methods`.

A value BPC leaves blank, or does not record for that kind of cancer (for example, a later distant metastasis for a cancer that is not the patient's first BPC Project Cancer), is unknown. Eligibility criteria then treat it as "Unknown values in eligibility criteria" says. When BPC does not record a concept the question needs at all, or the requested estimand cannot be estimated from the data, return the `unanswerable` result described under "Answer-type schemas".

- **Sources.** Use the analytic datasets (`*_dataset*.csv`) and the variables the data dictionary and the Analytic Data Guide define. The cBioPortal `data_clinical_supp_survival*.txt` files repeat some endpoints in another format and are not the canonical source.
- **Key index cancer.** Index cancers are the rows of `cancer_level_dataset_index.csv` with `redcap_ca_index == 'Yes'`. An index cancer is sequenced if it has a panel test in `cancer_panel_test_level_dataset.csv` (joined on `record_id` + `ca_seq`). A panel test that BPC lists under more than one cancer, because it could not be tied to a single diagnosis, belongs to each cancer it is listed under. "Earliest" orders cancers by `dob_ca_dx_days` where present, otherwise `ca_seq`. In these cohorts every patient's earliest index cancer is sequenced. A cancer's specimens are the `cpt_genie_sample_id`s for its `record_id` + `ca_seq`.
- **Stage at diagnosis.** `stage_dx`. Its values are Stage 0, I, II, III, and IV, "Stage I-III NOS" (coarse: unknown for any grouping that separates Stages I, II, and III), and blank. BPC does not record substage for curated cancers; `best_ajcc_stage_cd` is a free-text registry stage group, populated only for cancers taken from the tumor registry.
- **Later distant metastasis.** BPC records stage only at diagnosis. A distant metastasis after a Stage I–III diagnosis is `dmets_stage_i_iii == 1`, dated by `dx_to_dmets_days` (days from diagnosis); `pfs_cohort` groups cancers as "Stage IV" or "Stage I-III with Distant Mets". `dmets_stage_i_iii` is populated only for cancers that were Stage I–III at diagnosis and are the patient's first BPC Project Cancer; for any other cancer, a later distant metastasis is unknown.
- **Advanced disease.** BPC's advanced-disease variables (`*_adv_*`) define advanced disease as Stage IV at diagnosis or a distant metastasis after a Stage I–III diagnosis, dated at diagnosis or at the first distant metastasis. Use them only when the question defines advanced disease this way.
- **Histology.** `ca_hist_adeno_squamous` gives BPC's curated broad histology category; use it when the question adopts the data source's curated categories. When the question defines histology by ICD-O-3 codes, use the registry morphology code (`naaccr_histology_cd`). The Analytic Data Guide says this field is blank for curated cancers, but it is populated for most; use the values present. The specimen-level `path_ca_hist1`–`path_ca_hist25` fields hold morphology codes, although the Guide calls them topography codes.
- **Regimens.** Regimens are the rows of `regimen_cancer_level_dataset.csv`; drugs are combined when they share a row. A regimen contains drug X if its `regimen_drugs` string contains X (case-insensitive substring). Trial regimens are those with `drugs_ct_yn == 'Yes'`, whether `regimen_drugs` masks all, some, or none of their drugs (for example, as "Investigational Drug"); institutions differ in which trial drugs they mask. The unnamed-drug entries are "Other antineoplastic", "Other hormone", and "Other NOS". A regimen belongs to the cancer given by its `ca_seq`; a regimen with `ca_seq` missing is tied to no cancer. Number lines with `regimen_number_within_cancer`, not `regimen_number`, which counts regimens for all cancers. Regimen start is `dx_reg_start_int` (days from diagnosis). BPC curated every cancer-directed regimen, so a patient with no regimen row for the key index cancer was untreated.
- **Genomic files.** Mutations are in `data_mutations_extended.txt` (`Hugo_Symbol`, `Variant_Classification`). A specimen was tested for gene X if `data_gene_matrix.txt` (or the panel column in `data_clinical_sample.txt`, with the `data_gene_panel_*` files) shows its panel covers X. A specimen has copy-number results only if it has a column in `data_CNA.txt`. Fusions and structural variants are in `data_fusions.txt` and `data_sv.txt`, where present.
- **NGS testing date.** `dx_cpt_rep_days` in `cancer_panel_test_level_dataset.csv`, which counts days from diagnosis of the cancer. To place it on another time scale, subtract that origin's days from diagnosis (for regimen start, `dx_reg_start_int`).
- **Overall survival.** Use the BPC OS variable for the stated index date: `tt_os_dx_*` from diagnosis, `tt_os_g_*` from regimen start, and `tt_os_adv_*` from advanced disease when the question defines advanced disease as BPC does. Otherwise derive OS from the same death and last-known-alive dates, censoring at last known alive.
- **Named endpoints.** PFS-I, PFS-M, PFS-I-or-M, and PFS-I-and-M are BPC's `*pfs_i_*`, `*pfs_m_*`, `*pfs_i_or_m_*`, and `*pfs_i_and_m_*` variables: `*_adv_*` from advanced disease and `*_g_*` from regimen start. A unit whose variable is blank is outside that endpoint's population and is dropped. Because `dmets_stage_i_iii` is recorded only for the first BPC Project Cancer, a later index cancer that was Stage I–III at diagnosis has no PFS.
- **Patient-level attributes.** `patient_level_dataset.csv`, one row per patient.

---

## File-format reminders

- **`data_clinical_*.txt`** — TAB-DELIMITED, with leading `#`-prefixed
  metadata rows (four in the patient and sample files, five in the
  supplementary survival files). Use
  `pd.read_csv(..., sep='\t', comment='#', low_memory=False)`. The first
  non-`#` row carries machine-name column IDs (PATIENT_ID, CA_GRADE,
  etc.).

- **`data_timeline_*.txt`** — TAB-DELIMITED, no `#` rows.

- **`data_mutations_extended.txt`**, **`data_CNA.txt`**,
  **`data_fusions.txt`**, **`data_sv.txt`** (where present) — TAB-DELIMITED, no `#` rows.

- **`*_dataset*.csv`** — comma-delimited, properly quoted.

---

## Answer-type schemas

Return the answer type the question calls for. All numeric
fields are JSON numbers; integer counts are JSON integers.

If BPC cannot support the question (it does not record a concept the
question needs, or the requested estimand cannot be estimated from it),
return the `answer_type` the question calls for, with an `answer` of this
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

**Full output:**

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

Reply with ONLY the required JSON object on the LAST message. Do not wrap it in prose; do not append commentary after it. The
harness extracts JSON from the final assistant turn (tolerating Markdown
fences) and will reject responses with extra trailing text.
