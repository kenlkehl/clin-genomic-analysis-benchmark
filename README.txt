===============================================================================
Clinical Genomic Analysis Benchmark — Orientation for Colleagues
Last updated: 2026-06-20
===============================================================================

WHAT THIS BENCHMARK IS
-------------------------------------------------------------------------------
This benchmark measures how well an AI agent can interpret, disambiguate, and perform
clinico-genomic data analyses in oncology, using real cohorts from AACR Project
GENIE BPC (Biopharma Collaborative). It is built around a curated bank of
analysis questions, each with a "gold standard" verdict and (where applicable) a
pre-computed reference answer.

The core idea: a competent biostatistician/oncologist, handed a question and the
cohort data, would either (a) recognize that the question is too underspecified
to answer deterministically ("ambiguous") and say what must be pinned down, or
(b) recognize it as answerable under standard conventions ("unambiguous") and
compute the one conventional answer. The benchmark tests whether an agent does
the same.

Six cohorts are used (folder name = cohort id):
   bladder_1.2, breast_1.2, crc_2.0_public, nsclc_2.0_public,
   panc_1.2, prostate_1.2

Current question bank: bpc_benchmark_review_6-19-26.xlsx
   211 questions total: 96 unambiguous, 115 ambiguous
   (~34-36 questions per cohort)


THE TASK WE GIVE THE AGENT (three stages)
-------------------------------------------------------------------------------
Each question is served to the agent in up to three stages. The agent is given:
the question text, a read-only copy of the cohort's data directory, the cohort's
data dictionary, and a scratch directory. All agent outputs are JSON.

  1. CLASSIFY
     Decide UNAMBIGUOUS vs AMBIGUOUS. Unambiguous = two careful analysts using
     standard oncology/biostatistics conventions would compute the SAME answer.
     Ambiguous = the question leaves a *material* analytic choice open that
     competent analysts would resolve differently, changing the result.

  2. DISAMBIGUATE  (only if the agent said AMBIGUOUS)
     List the concrete concepts a questioner would have to specify to make the
     question deterministically answerable (short noun-phrases; ~3-6 of them).

  3. ANALYZE  (only if the agent said UNAMBIGUOUS)
     Actually compute the answer from the cohort data files (Python: pandas,
     numpy, scipy, statsmodels, lifelines) and return it as typed JSON.

Scoring rewards: correct ambiguous/unambiguous classification; naming the right
gaps when ambiguous; and computing the correct conventional answer when
unambiguous.


AGENT_INSTRUCTIONS.md  (the rulebook shown to the agent)
-------------------------------------------------------------------------------
This is the single document the agent under evaluation is expected to follow. It
is written to be answer-agnostic (it never reveals answers to specific
questions). It contains:

  * "How to score highly" — a short upfront checklist of the decision rules.
  * The three stages and the exact JSON output schema for each.
  * Default rule for classification — lean UNAMBIGUOUS when a competent analyst
    would reach for one column, one filter, one method without asking; only flag
    AMBIGUOUS for a genuine, material gap.
  * Conventional defaults — things the agent must ASSUME (and therefore must NOT
    flag as ambiguous), e.g.:
        - non-synonymous mutation definition; "mutation in gene X"; any-positive
          patient aggregation; panel-coverage lookup.
        - "regimen containing drug X" = case-insensitive substring; biosimilars
          count as the reference molecule; distinct-INN ADCs do not.
        - KEY INDEX CANCER: unless a question explicitly says otherwise, each
          patient is represented by a single "key index cancer" = their first
          (earliest) genomically profiled index cancer. Question stems will
          generally NOT contain the word "index"; "patients with <cohort>
          cancer" already means this key index cancer.
        - GENOMIC-SPECIMEN SCOPE: genomic facts come from specimens of the
          cohort's cancer. Patient-level genomic questions use the key index
          cancer's specimen; sample-level counts use all of the patient's
          cohort-cancer specimens.
        - default statistics: univariable Cox (lifelines) for HRs; KM +
          Brookmeyer-Crowley for time-to-event medians; Wilson CIs for
          proportions; canonical OS/time-origin variables.
  * "Avoid flagging as ambiguous on these grounds alone" — the flip side of the
    conventions.
  * "What still counts as AMBIGUOUS" — the genuine triggers, e.g. unanchored
    line-of-therapy; undefined outcome metric (incl. the PFS-I / PFS-M /
    PFS-I-or-M / PFS-I-and-M definitions); missing/underivable comparator;
    cancer-level anatomic/histologic scope in a heterogeneous cohort; biomarker
    definition; biomarker-frequency denominator (all sequenced vs gene-covered);
    missing time origin; filter cut-offs; DELAYED COHORT ENTRY (left truncation
    at sequencing date); and IMMORTAL TIME BIAS (needs a landmark analysis).
  * File-format reminders (the GENIE/cBioPortal .txt files have leading
    "#"-comment rows; the *_dataset*.csv files are plain CSV).
  * Answer-type schemas + output discipline (return ONLY the JSON object).


THE REVIEW EXCEL FILE  (bpc_benchmark_review_6-19-26.xlsx)
-------------------------------------------------------------------------------
This is the authoritative question bank + gold standard + review trail. Two
sheets:

  * "questions" — one row per benchmark question (211 rows). Columns:

      A  qid                     Unique question id, "<cohort>-Q<hash>".
      B  cohort                  Which BPC cohort.
      C  category                Numeric category tag (question-type taxonomy).
      D  classification          "unambiguous" or "ambiguous".  <-- the gold verdict
      E  question_text           The question exactly as posed to the agent.
      F  disambiguation_concepts (AMBIGUOUS rows) newline-separated list of the
                                 concepts that must be specified. The gold set of
                                 "gaps" for the disambiguate stage.
      G  analysis_plan_summary   (UNAMBIGUOUS rows) ~80-200 word prose spec an
                                 analyst could follow alone to reproduce the
                                 analysis: population unit, every eligibility
                                 filter, arm composition, statistic, and
                                 time-to-event / censoring / left-truncation
                                 rules. This is the human-readable gold method.
      H  expected_answer_type    (UNAMBIGUOUS) one of: count, proportion,
                                 median_with_ci, hazard_ratio_with_ci,
                                 odds_ratio_with_ci, pvalue, categorical,
                                 categorical_distribution. Determines the shape
                                 of the gold_answer.
      I  gold_answer             (UNAMBIGUOUS) the pre-computed reference answer
                                 as JSON, with fields matching expected_answer_type.
                                 This is exactly what the gold script prints.
      J  gold_script             (UNAMBIGUOUS) path (relative to repo root) of the
                                 Python script that produced gold_answer.


    Conventions inside the sheet:
      - Rows are tinted white for unambiguous, pale orange for ambiguous.
      - For an UNAMBIGUOUS row, columns G/H/I/J are filled and F is blank.
      - For an AMBIGUOUS row, column F is filled and G/H/I/J are blank.

  * "legend" — a field glossary. NOTE: its summary "Note" lines (e.g. "221
    questions") are stale from an earlier version; the live counts are 211
    questions / 96 unambiguous / 115 ambiguous as above.


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
        "bpc_from_synapse/<cohort>" \
        "gold_standard/<cohort>/<qid>.result.json"

  The gold answers are reproducible: re-running a script must reproduce the
  gold_answer stored in the workbook (verified to 0 diffs after each revision).

  NOTE: the gold_standard/ folders may also contain leftover scripts from
  questions that were later removed or reclassified to ambiguous. The
  authoritative list of *active* gold scripts is the set of paths in the
  gold_script column (J) of the current workbook.


THE UNDERLYING DATA  (bpc_from_synapse/)
-------------------------------------------------------------------------------
One read-only directory per cohort, in standard GENIE BPC + cBioPortal format.
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
  variable synopsis (xlsx) — the data dictionary the agent is given.

  (Tab-delimited data_clinical_*.txt / data_timeline_*.txt have FIVE leading
  "#"-comment rows; read with sep='\t', comment='#'. The *_dataset*.csv files
  are ordinary CSV.)


HOW IT FITS TOGETHER
-------------------------------------------------------------------------------
  - To understand the task an agent faces:        read AGENT_INSTRUCTIONS.md.
  - To see the questions + gold verdicts/answers: open the review .xlsx.
  - To see/run how a gold answer was computed:    open gold_standard/<cohort>/<qid>.py.
  - To inspect the data being analyzed:           browse bpc_from_synapse/<cohort>/.

A typical evaluation: serve column E (question_text) to the agent with the cohort
directory; compare the agent's classify verdict to column D; for ambiguous, score
the agent's concepts against column F; for unambiguous, score the agent's computed
answer against column I (gold_answer).


REVISION NOTES
-------------------------------------------------------------------------------
The bank has been refined over several oncologist-reviewed rounds. The most recent round (6-8 review,
applied 2026-06-19) standardized the "key index cancer" framing across question
stems, added the genomic-specimen-scope and immortal-time-bias conventions to
AGENT_INSTRUCTIONS.md, removed 7 low-value/duplicative questions, and
reclassified several questions after subtype-scope clarifications. The current
bank file is bpc_benchmark_review_6-19-26.xlsx.
===============================================================================
