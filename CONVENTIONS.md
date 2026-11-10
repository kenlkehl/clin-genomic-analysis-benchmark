# Conventions

These conventions fix the meaning of common terms in the benchmark's questions. When a question uses one of these terms without further qualification, assume the convention listed. A choice a convention settles is not a gap. The conventions are stated without reference to any dataset.

## Population and cancer

- **Unknown values in eligibility criteria.** Eligibility criteria apply only to units known to meet them. A criterion that requires a value (for example, "Stage IV at diagnosis" or "with a mutation in gene X") excludes units whose value is unknown, and a criterion that excludes a value (for example, "excluding patients with brain metastases") keeps them.
- **Key index cancer.** A patient's index cancers are their cancers of the cohort's type. Unless a question *explicitly* asks about several index cancers per patient, each patient's analysis uses one **key index cancer**: the **earliest-diagnosed sequenced index cancer that meets the question's cancer-level eligibility criteria** (for example, stage at diagnosis, histology, a biomarker found on that cancer's specimens, or a qualifying regimen given for that cancer). A later, separate primary that meets the criteria therefore makes the patient eligible even when an earlier index cancer does not, unless the question excludes patients with prior primary cancers. A cancer-level characteristic that is the outcome or a covariate, rather than an eligibility criterion, does not choose the key index cancer: it is measured on the key index cancer the eligibility criteria choose. When the question has no cancer-level criteria, the key index cancer is the earliest sequenced index cancer. Population counts, treatment questions, and genomic questions all focus on the key index cancer. A bare reference to the cohort's cancer, by name or usual abbreviation, means the key index cancer; it is not a filter on histology or site. The existence of several index cancers, and which index cancer a regimen or specimen belongs to, are settled by this convention and are not, by themselves, gaps.
- **Other primary cancers.** Patients who also had other primary cancers, before or after the key index cancer, are analysed unless the question excludes them.
- **Genomic specimens.** Genomic results (mutations, copy-number changes, fusions) come only from specimens of the patient's index cancers; a specimen sequenced for a cancer of another type never counts. Patient-level genomic questions ("does the patient's tumor have X", "what fraction of patients have X") read the specimens of the key index cancer. Specimen-level questions that count specimens ("how many sequenced samples have X") include every specimen of any of the patient's index cancers. "Patients whose tumors were sequenced" means patients who have such a specimen.
- **Patient-level attributes.** Questions about patient-level attributes (sex, race, ethnicity, vital status) are answered once per patient. Histologic or site scope does not apply to them and is not a gap.
- **Treatment attribution.** Only regimens given for the key index cancer count, unless the question names other cancers (for example, "for any cancer"). Wording about time ("ever", "at any point") does not widen this. A regimen that cannot be tied to any cancer does not count. Lines of therapy are numbered only among regimens for the key index cancer, so a regimen for another cancer, or one tied to no cancer, does not move a later regimen to a later line.
- **No recorded treatment.** A patient with no recorded cancer-directed regimen for the key index cancer received none. Count such patients as untreated, not as missing.

## Stage

- **Stage at diagnosis.** The overall stage group at initial diagnosis of the cancer: the best available combination of clinical and pathologic staging, under the staging edition in use at diagnosis. A patient "diagnosed at" a stage (for example, "diagnosed at Stage IV") has that stage at diagnosis. A reference to stage that does not explicitly tie it to the initial diagnosis ("at diagnosis", "at initial diagnosis", "diagnosed at", "de novo", "newly diagnosed", or similar) leaves open whether it means stage at diagnosis. That includes "diagnosed with Stage IV" and "after Stage IV diagnosis", because a distant recurrence is also diagnosed.
- **Metastatic.** Distant metastasis. Spread to regional lymph nodes alone is not metastatic disease. Whether "metastatic" includes only de novo metastatic disease, or also distant recurrence, is left open.

## Genomics

- **Non-synonymous mutation.** A variant whose MAF `Variant_Classification` is one of Missense_Mutation, Nonsense_Mutation, Frame_Shift_Del, Frame_Shift_Ins, Splice_Site, In_Frame_Del, In_Frame_Ins, Nonstop_Mutation, or Translation_Start_Site. This excludes Silent, intronic, UTR, RNA, IGR, and Splice_Region variants.
- **Mutation in gene X.** Any non-synonymous mutation in gene X reported by tumor sequencing; the question need not say whether it is somatic. "X-mutant" and "X-mutated" mean the same, unless clinicians commonly read that gene's mutant label as a specific set of variants (for example, the activating or drug-sensitizing variants of a gene whose targeted therapies act only on those). Then, as with drug classes (see "Drug classes"), the question must list the variants that count, or say "any non-synonymous mutation".
- **Patient-level aggregation.** A patient is positive if any of the key index cancer's sequenced specimens carries the qualifying alteration (any-positive aggregation). Alterations described as co-occurring (for example, a "co-mutation") are each read this way, at the patient level; they need not be on the same specimen.
- **Tested for gene X.** A patient or specimen was tested for gene X only if the assay that sequenced it covers X, whether or not the question says so. Patients not tested for a gene are excluded, not treated as wild-type, when eligibility or the exposure is a mutation in that gene; for eligibility this follows "Unknown values in eligibility criteria".
- **Copy-number results.** A specimen has copy-number results only if its assay reports copy number; some assays do not, even when their panels include the gene. A biomarker defined wholly or partly by copy number is assessed only in specimens with copy-number results; patients without them are excluded, as untested patients are for mutations.
- **NGS testing date.** The date of the patient's earliest sequencing report for the key index cancer.

## Treatment

- **Regimens and combinations.**
  - A regimen is a set of drugs given together as one course of treatment. Drugs are combined, or concurrent, when they belong to the same regimen.
  - "Regimen containing drug X" means any regimen that includes X, with or without other drugs. "Regimen containing X and Y" means both drugs are present, other drugs allowed.
  - A combination named without "containing", such as "X plus Y" or "X with Y", leaves open whether regimens with additional drugs qualify. Naming an optional drug ("X plus Y, with or without Z") settles only Z; whether regimens that add any other drug qualify stays open. Only wording that excludes other drugs ("only", "exactly", "and no other drugs") or allows them ("containing", "with or without other drugs") settles it.
  - "Single-agent", "monotherapy", and "as the only drug" mean exactly one drug. Excluding a drug or class ("without concurrent immunotherapy") settles only that drug or class, as naming an optional drug does.
- **Drugs.** A named drug covers its biosimilars; whether it covers its biosimilar or prodrug forms is not, by itself, a gap. Antibody-drug conjugates that share an antibody but carry different payloads are different drugs.
- **Drug classes.** A drug family whose members clinicians agree on (for example, platinum, taxane, PD-1/PD-L1 inhibitor, or immune checkpoint inhibitor) means those members and is not a gap. A family whose membership clinicians commonly read differently must list its drugs: for example, "EGFR inhibitor" is often read as the EGFR tyrosine kinase inhibitors alone, though antibodies such as cetuximab and necitumumab also target EGFR. So must a broad term (for example, chemotherapy, immunotherapy, targeted therapy, or endocrine therapy). If such a term does not list its drugs or otherwise say which count, the component that uses it needs clarification. A data source's grouping of drugs into kinds does not define class membership.
- **Unspecified drugs.** A record of an unnamed drug (for example, "other antineoplastic" or "other hormone") never matches a named drug or a drug class. A patient whose only regimens use unnamed drugs did not receive any named drug or class.
- **Clinical-trial regimens.** A regimen given on a clinical trial is a trial regimen whether or not its record names every drug.
- **"Assume any clinical-trial regimen did not contain drug X"** (or drugs X, Y, …). Treat every trial regimen as not containing X (or any of the named drugs), even when its record names X.
- **Receipt.** A patient received a drug if they started it, however long it was given.
- **Outcomes of treated patients.** When every patient analysed has a qualifying regimen (one regimen group, regimens compared with each other, or groups compared among recipients of a regimen), time-to-event outcomes are measured from the start of that regimen unless the question says otherwise.

## Endpoints

- **Survival.** Overall survival, when the question names no other endpoint.
- **Overall survival.** Time from the index date to death from any cause, censored at the last known alive date.
- **Progression-free survival.** Death without progression is an event. Everything else about a progression endpoint (how progression is ascertained, and censoring) must be stated, or fixed by a named endpoint.
- **Survival at a time point.** A survival probability or rate at a stated time ("2-year overall survival") is the Kaplan–Meier estimate at that time, with the lifelines default (log(-log)) confidence interval.

## Named endpoints

These endpoint names come from the BPC data source. A question that uses one adopts its definition: its events, its censoring, and the units for which it is defined. A unit outside an endpoint's population is not analysed. The question must still fix everything else, including how patients enter the risk set. The name does not define the question's own words: "PFS-I from onset of advanced disease" still needs "advanced" defined by its stage groups, and the endpoint's advanced-disease index date applies only when the question defines advanced disease as below.

- **PFS-I (imaging).** An event is progression documented in an imaging report (the radiologist's assessment that the cancer is progressing, worsening, or enlarging), or death.
- **PFS-M (medical oncologist).** An event is progression documented in a medical oncologist's assessment, or death.
- **PFS-I-or-M.** An event is the first progression documented by either source, or death.
- **PFS-I-and-M.** An event is progression documented by both sources (that is, the point at which progression has previously been documented by both sources, which may not necessary have documented the progression at the same time), or death.

Each is defined from two index dates, and only for cancers that were Stage IV at diagnosis, or that were Stage I–III at diagnosis and later developed a distant metastasis:

- **From the onset of advanced disease** (the diagnosis date for Stage IV at diagnosis, or the date of the first distant metastasis after a Stage I–III diagnosis): censored at the last known alive date.
- **From the start of a cancer-directed regimen:** censored at the start of the next cancer-directed regimen if there is one, otherwise at the last known alive date. For cancers that were Stage I–III at diagnosis, defined only for regimens that began after the first distant metastasis.

With any other index date, a name fixes only the events, and the question must state the censoring date.

## Statistics

- **Hazard ratio / Cox model.** A hazard ratio means an unadjusted (univariable) Cox proportional-hazards model from `lifelines` (with its default handling of ties), unless the question names covariates, says "adjusted", or specifies another model structure. Wording about delayed entry or left truncation (for example, "risk-set adjustment for delayed entry") sets how patients enter the risk set; it does not make the model multivariable. The p-value for a hazard ratio is the Wald p-value for that coefficient in the fitted model's summary (`CoxPHFitter.summary['p']`).
- **Model terms.** In a multivariable model, a continuous adjustment variable enters linearly on its recorded scale, and a categorical one enters as indicator terms.
- **Reference group.** In "A versus B" or "A compared with B", B is the reference group. In "with versus without X", the group without X is the reference.
- **Left truncation mechanics.** When left-truncating at the sequencing report date, a patient enters the risk set at that date on the outcome's time scale, or at the time origin if sequencing came first. A patient whose entry time is on or after their event or censoring time is never at risk: drop that patient rather than shifting either time.
- **Landmarks.** In a landmark analysis, the patients analysed are those alive and under follow-up at the landmark, time is measured from the landmark, and an exposure is fixed at its status at the landmark.
- **Median with 95% CI for time-to-event.** Kaplan–Meier median from lifelines `KaplanMeierFitter` (default `alpha=0.05`), with the CI from `median_survival_times(kmf.confidence_interval_)`: the Brookmeyer–Crowley method applied to lifelines' default log(-log) pointwise band.
- **Proportion with 95% CI.** Wilson score interval (`statsmodels` `proportion_confint(..., method="wilson")`).
- **Median with 95% CI for a continuous variable that is not a time to event (for example, age).** Exact distribution-free CI (binomial order statistics), or equivalently the lifelines/scipy default.
- **Tests and intervals.** P-values are two-sided, and confidence intervals are 95%, unless the question says otherwise.
- **Correlated units.** When a patient contributes several units (for example, several regimens), a clearly specified question must specify a plan for dealing with repeated measures within each patient.

## Units, cutoffs, and ranges

- **Time units.** A month is 30.4375 days and a year is 365.25 days.
- **Cutoffs and ranges.** Cutoffs are read literally: "65 or older" and "≥ 65" include 65; "over 65" and "older than 65" do not. A range of dates or years includes both ends ("diagnosed 2015–2017" includes 2017).
