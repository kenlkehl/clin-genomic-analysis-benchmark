===============================================================================
Clinical Genomic Analysis Benchmark — Orientation for Colleagues
Last updated: 2026-11-04
===============================================================================

WHAT THIS BENCHMARK IS
-------------------------------------------------------------------------------
This benchmark measures two things, as two separate tasks: how well an AI
model can judge whether a clinico-genomic analysis question in oncology is
specified well enough to answer (ASSESS), and how well an AI agent can perform
the analysis of a question that is (ANALYZE), using real cohorts from AACR
Project GENIE BPC (Biopharma Collaborative). It is built around a curated bank of analysis
questions, each with a "gold standard" verdict and (where applicable) a
pre-computed reference answer.

The core idea: a competent biostatistician/oncologist, handed a question, checks
four parts of it before computing anything:

  1. the DENOMINATOR  - who (or what) is analysed: cancer type and histology,
                        disease burden (for example, de novo versus recurrent
                        metastatic disease), line of therapy, and what to do
                        with patients not yet sequenced at a key time point;
  2. the OUTCOME      - what is measured; for survival-type outcomes, the
                        starting date, how progression or response is judged
                        (radiologist, oncologist, both, or either), and how
                        follow-up is censored;
  3. the COVARIATES   - the exposures, comparison groups and adjustment
                        variables, each defined so that only one version can be
                        built from the data;
  4. the ANALYSIS PLAN - left truncation where it applies, the statistic to
                        report, the variables in any multivariable model, and
                        the type of model.

If any of the four needs clarification, the question is "ambiguous" and the
analyst would ask before proceeding. If all four are clear, the question is
"unambiguous" and the analyst computes the one answer it calls for. The
benchmark tests whether a model does the same.

That check is about what the researcher meant, not about any one dataset.
Datasets define their own terms: BPC, for example, records stage only at
diagnosis, but a researcher who writes "Stage IV disease" may also mean
patients whose earlier-stage cancer later spread. So the ASSESS task is judged
from the question's words and the rulebook alone, without the data.

Six cohorts are used (folder name = cohort id):
   bladder_1.2, breast_1.2, crc_2.0_public, nsclc_2.0_public,
   panc_1.2, prostate_1.2

Current question bank: bpc_benchmark_review_6-19-26.xlsx
   205 questions (~33-36 per cohort). From 2026-10-25 every question is being
   re-reviewed, and possibly edited, under the four-part rubric above; the
   four-part gold verdicts are filled in as the review proceeds, and gold
   answers are recomputed where questions change.


THE TWO TASKS
-------------------------------------------------------------------------------
The two tasks are run separately, usually with different models. All outputs
are JSON.

  1. ASSESS  (every question; a single prompt, no tools, no data)
     The model receives one prompt: the assess rulebook, then the question and
     the cohort's cancer type. For each of the four parts (denominator,
     outcome, covariates, analysis plan), it says whether the part needs
     clarification, with a one- or two-sentence reason. A part needs
     clarification when the question leaves a choice open that no convention
     settles (for example, "Stage IV" that does not say "at diagnosis", "PFS"
     that does not say who judges progression, or no rule for unknown values
     of a grouping variable). This is judged from the wording alone, not from
     what any dataset records or whether the choice would change the result.

  2. ANALYZE  (every gold-unambiguous question; an agent with the data)
     The agent is given the question text, a read-only copy of the cohort's
     data directory, the cohort's data dictionary, and a scratch directory. It
     computes the answer from the cohort data files (Python: pandas, numpy,
     scipy, statsmodels, lifelines) and returns it as typed JSON. It is told
     the question is fully specified; it is not given the gold verdicts.

HOW IT IS SCORED
-------------------------------------------------------------------------------
All scoring is deterministic and makes no LLM or network calls. Each run is
scored on its own; the headline combines one ASSESS run and one ANALYZE run,
half each (they must cover the same cohorts and be scored against the same
gold).

  * ASSESS: each of the agent's four verdicts is compared with the gold verdict
    for that part. The reasons are kept for auditing but not scored. Because
    most parts of most questions are clear, an agent that simply said "clear"
    every time would look good on plain accuracy. So for each part we use
    balanced accuracy: the average of (how often the agent caught a part that
    needs clarification) and (how often it correctly left a clear part alone).
    Saying "clear" every time scores 50%, as does saying "unclear" every time.
    The ASSESS score is the average over the four parts. If the model never
    returns a valid reply for a question, all four verdicts count as wrong.

  * ANALYZE: the agent's number is compared with the gold answer and earns
    2 (within 5%), 1 (5-15% off) or 0 points. The ANALYZE score is points
    earned out of 2 per gold-unambiguous question; a question the agent fails
    to answer earns 0.

  * A question that is perfectly well specified but cannot be estimated from
    the data (for example, a model that cannot be fit because one group is
    nearly empty) stays unambiguous, so it is in the ANALYZE task; the agent
    earns full credit only by reporting it as "unanswerable".


THE RULEBOOKS  (ASSESS_INSTRUCTIONS.md, ANALYZE_INSTRUCTIONS.md, CONVENTIONS.md)
-------------------------------------------------------------------------------
Each task has its own rulebook, and both share the conventions. They are
written to be answer-agnostic (they never reveal answers to specific
questions).

  * ASSESS_INSTRUCTIONS.md  (shown to the assess model, followed by the
    conventions and then the question):
        - a short checklist and the exact JSON output schema;
        - how to judge clarity from the researcher's words rather than a
          dataset, and how a question can settle a choice by naming a
          definition (a published standard such as AJCC or RECIST, a named
          endpoint such as PFS-I, or the data source's own definition);
        - for each of the four parts, a catalog of the choices a question has
          to fix, and which wording fixes or leaves them open;
        - which components a gap touches, and what not to flag.
  * ANALYZE_INSTRUCTIONS.md  (shown to the analyze agent, followed by the
    conventions):
        - how to map each concept onto the BPC files and variables (for
          example, stage at diagnosis is stage_dx, and lines of therapy are
          counted with regimen_number_within_cancer);
        - file-format reminders (the GENIE/cBioPortal .txt files have leading
          "#"-comment rows; the *_dataset*.csv files are plain CSV);
        - answer-type schemas and output discipline (return ONLY the JSON).
  * CONVENTIONS.md  (both): things to ASSUME when a question does not say,
    e.g.:
        - non-synonymous mutation definition; "mutation in gene X"; any-positive
          patient aggregation; panel coverage defines who was tested.
        - "regimen containing drug X" allows other drugs; "X plus Y" leaves
          that open; biosimilars count as the reference molecule.
        - KEY INDEX CANCER: unless a question explicitly says otherwise, each
          patient is represented by a single "key index cancer" = their
          earliest sequenced cancer of the cohort's type that meets the
          question's cancer-level criteria (so a later, separate primary
          that meets them counts, unless the question excludes patients
          with prior primaries). Question stems will generally NOT contain
          the word "index"; "patients with <cohort> cancer" already means
          this key index cancer.
        - GENOMIC-SPECIMEN SCOPE: genomic facts come from specimens of the
          cohort's cancer. Patient-level genomic questions use the key index
          cancer's specimens; sample-level counts use all of the patient's
          cohort-cancer specimens.
        - outcomes of treated patients are measured from the start of the
          treatment unless the question says otherwise.
        - a hazard ratio means an unadjusted Cox model (lifelines) unless the
          question names covariates, says "adjusted", or names another model.
        - KM + Brookmeyer-Crowley for time-to-event medians; Wilson CIs for
          proportions; a month is 30.4375 days.
        - the named PFS endpoints (PFS-I, PFS-M, PFS-I-or-M, PFS-I-and-M) and
          what each counts as progression.


THE REVIEW EXCEL FILE  (bpc_benchmark_review_6-19-26.xlsx)
-------------------------------------------------------------------------------
This is the authoritative question bank + gold standard + review trail. The
default path is:

  ~/Partners HealthCare Dropbox/Kenneth Kehl/chatbpc/chatbpc_benchmark_gold/
    bpc_benchmark_review_6-19-26.xlsx

Its main sheets:

  * "questions" - one row per benchmark question (205 rows). Key columns:

      qid                     Unique question id, "<cohort>-Q<hash>".
      cohort                  Which BPC cohort.
      category                Numeric category tag (question-type taxonomy).
      classification          "unambiguous" or "ambiguous".  <-- the gold verdict
      question_text           The question exactly as posed to the agent.
      denominator_needs_clarification, outcome_needs_clarification,
      covariates_needs_clarification, analysis_plan_needs_clarification
                              Y or N for each of the four parts. These are the
                              gold the ASSESS task is scored against. They are
                              filled in during the 2026-10 re-review; a blank
                              row has not been re-reviewed yet and cannot be
                              scored. "ambiguous" must mean at least one Y.
      <part>_notes            Beside each flag: what is unclear, or how the
                              question pins that part down. For reviewers only.
      analysis_plan_summary   (UNAMBIGUOUS rows) ~80-200 word prose spec an
                              analyst could follow alone to reproduce the
                              analysis: population unit, every eligibility
                              filter, arm composition, statistic, and
                              time-to-event / censoring / left-truncation
                              rules. This is the human-readable gold method.
      expected_answer_type    (UNAMBIGUOUS) one of: count, proportion,
                              median_with_ci, hazard_ratio_with_ci,
                              odds_ratio_with_ci, pvalue, categorical,
                              categorical_distribution. Determines the shape
                              of the gold_answer.
      gold_answer             (UNAMBIGUOUS) the pre-computed reference answer
                              as JSON, with fields matching expected_answer_type.
                              This is exactly what the gold script prints.
      gold_script             (UNAMBIGUOUS) path (relative to gold root) of the
                              Python script that produced gold_answer.
      disambiguation_concepts, disambiguation_concept_ids
                              RETIRED. The earlier task asked agents to pick
                              ambiguity "concepts" from a fixed menu. These
                              columns are kept as history and reviewer context
                              and are no longer scored.

    Conventions inside the sheet:
      - Rows are tinted white for unambiguous, pale orange for ambiguous.
      - For an UNAMBIGUOUS row, the analysis plan, answer type, gold answer and
        gold script are filled.

  * "legend" - a field glossary. Its summary counts are from earlier versions
    of the bank; the live counts are in the "questions" sheet.

  * "concept_menu" - RETIRED: the old menu of ambiguity concepts, kept as
    history.


THE GOLD STANDARD SCRIPTS  (gold_standard/)
-------------------------------------------------------------------------------
For every UNAMBIGUOUS question there is a self-contained Python script that
computes the reference answer directly from the cohort data. Layout:

    gold_standard/<cohort>/<qid>.py            # the analysis
    gold_standard/<cohort>/<qid>.result.json   # its output (== gold_answer cell)

  Example:
    gold_standard/panc_1.2/panc_1.2-Qadb92d64.py
    gold_standard/prostate_1.2/prostate_1.2-Q5a10607f.py

  Each script:
    - is standalone (imports only pandas/numpy/scipy/lifelines/statsmodels);
    - defines  analyze(cohort_dir: Path) -> dict ;
    - has a __main__ block:  python <script> <cohort_dir> <out.result.json>
      reads the cohort data, computes the answer, writes JSON to out path;
    - returns the bare typed dict for its answer_type (e.g. {"value":..,"n":..}).

  Run one:
    .venv/bin/python "gold_standard/<cohort>/<qid>.py" \
        "$CLINGEN_DATA_ROOT/<cohort>" \
        "gold_standard/<cohort>/<qid>.result.json"

  The gold answers are reproducible: re-running a script must reproduce the
  gold_answer stored in the workbook (verified to 0 diffs after each revision).

  NOTE: the gold_standard/ folders may also contain leftover scripts from
  questions that were later removed or reclassified to ambiguous. The
  authoritative list of *active* gold scripts is the set of paths in the
  gold_script column (K) of the current workbook.


THE UNDERLYING DATA  ($CLINGEN_DATA_ROOT)
-------------------------------------------------------------------------------
One read-only directory per cohort, in standard GENIE BPC + cBioPortal format.
CLINGEN_DATA_ROOT has to be set to the directory that contains them.
Key files an analysis draws on:

    cancer_level_dataset_index.csv        one row per index cancer (redcap_ca_index)
    cancer_level_dataset_non_index.csv    non-index cancers
    cancer_panel_test_level_dataset.csv   sequencing tests (links cancer <-> sample)
    patient_level_dataset.csv             one row per patient (demographics, vitals)
    regimen_cancer_level_dataset.csv      systemic therapy regimens
    data_clinical_patient.txt / _sample.txt           clinical metadata
    data_mutations_extended.txt           somatic mutations (MAF)
    data_CNA.txt                          copy-number (-2 deep del .. +2 amp)
    data_fusions.txt / data_sv.txt        fusions / structural variants
    data_gene_matrix.txt, data_gene_panel_*.txt       panel coverage
    data_timeline_*.txt                   longitudinal events (treatment, imaging,
                                          path, sequencing, performance status...)
  Each cohort folder also includes its GENIE analytic data guide (PDF) and
  variable synopsis (xlsx) — the data dictionary the analyze agent is given.
  The assess model sees none of these.

  (Tab-delimited data_clinical_*.txt / data_timeline_*.txt have FIVE leading
  "#"-comment rows; read with sep='\t', comment='#'. The *_dataset*.csv files
  are ordinary CSV.)


HOW IT FITS TOGETHER
-------------------------------------------------------------------------------
  - To understand the assess task:                read ASSESS_INSTRUCTIONS.md
                                                  and CONVENTIONS.md.
  - To understand the analyze task:               read ANALYZE_INSTRUCTIONS.md
                                                  and CONVENTIONS.md.
  - To see the questions + gold verdicts/answers: open the review .xlsx.
  - To see/run how a gold answer was computed:    open gold_standard/<cohort>/<qid>.py.
  - To inspect the data being analyzed:           browse $CLINGEN_DATA_ROOT/<cohort>/.

A typical evaluation: run ASSESS on every question and compare the model's four
verdicts with the four *_needs_clarification columns; run ANALYZE on every
gold-unambiguous question and compare each computed answer with gold_answer;
then combine the two scores.


REVISION NOTES
-------------------------------------------------------------------------------
2026-11-04: assess and analyze became separate tasks. ASSESS is now one
prompt per question with no tools and no data, judged from the question's
words alone; its rulebook catalogs the choices each part must fix. ANALYZE
runs only on gold-unambiguous questions, whatever any assess run concluded.
The single AGENT_INSTRUCTIONS.md was split into ASSESS_INSTRUCTIONS.md,
ANALYZE_INSTRUCTIONS.md and the shared CONVENTIONS.md. Runs from before the
split are not comparable with new runs.

2026-10-25: the task was redesigned. The former CLASSIFY and DISAMBIGUATE
stages (an ambiguous/unambiguous label plus a pick-list of ambiguity concepts)
were replaced by one ASSESS stage with four verdicts (denominator, outcome,
covariates, analysis plan), scored by balanced accuracy. Every question is
being re-reviewed under the new rubric. Runs scored under the old task are not
comparable with new runs.

The bank has been refined over several oncologist-reviewed rounds. An earlier round (6-8 review,
applied 2026-06-19) standardized the "key index cancer" framing across question
stems, added the genomic-specimen-scope and immortal-time-bias conventions to
the rulebook (then AGENT_INSTRUCTIONS.md), removed 7 low-value/duplicative questions, and
reclassified several questions after subtype-scope clarifications. The current
bank file is bpc_benchmark_review_6-19-26.xlsx.
===============================================================================
