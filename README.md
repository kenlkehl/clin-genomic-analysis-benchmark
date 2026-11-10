# clin-genomic-analysis-benchmark

A benchmark for AI agents and coding assistants on **translational clinical cancer-data analysis**, built on AACR Project GENIE BPC (Biopharma Collaborative) cohort data.

Real analyses of clinico-genomic data fail in two different ways. Sometimes the question is clear and the agent computes the wrong number. More often the question *sounds* clear but isn't — "how did patients on first-line chemotherapy do?" hides a dozen choices about what counts as first line, what counts as chemotherapy, and what "did" means. A competent analyst notices and asks. This benchmark measures both, as two separate tasks:

- **assess** — can a model tell which parts of a question are underspecified: who is analysed, what is measured, which variables are compared or adjusted for, and how the estimate is computed? This is judged from the researcher's words alone, not from any dataset, because a dataset's definitions (of stage, of a regimen, of progression) do not tell you what the researcher meant. It is a single prompt with no tools and no data.
- **analyze** — given a question whose gold says every part is clear, can an agent compute the right number from the BPC cohort files? This is agentic: the agent explores the data and runs code.

> Not a developer? **`README.txt`** is a plain-language tour of the task, the review workbook, and the gold answers. This file is the operator's guide.

## Status

All six cohorts are wired end to end. The reference adapter drives Claude Code on `claude-opus-4-8` via Vertex.

| Component | Status |
|---|---|
| Question bank | **205 questions across six cohorts, being re-reviewed under the four-component rubric** (started 2026-10-25). Every question is reviewed and may be edited; component gold labels are pending, and scoring an assess run refuses a question until it has them (analyze runs use the gold classification and answer). Curated in `bpc_benchmark_review_6-19-26.xlsx` under `$CLINGEN_GOLD_ROOT` |
| Gold answers | Every unambiguous question has a computed, re-runnable gold answer. Answers are recomputed as questions are edited during the re-review. Questions that are well specified but not estimable are marked `unanswerable` (see below) |
| Agent-facing bank | `questions/<cohort>.yaml` — `id`, `category`, `text` only. The agent under test sees the `id` and `text`; `category` stays harness-side for per-category reporting |
| Gold bank | `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` — answers, per-component verdicts with reviewer notes, classifications, and legacy concept prose kept as unscored history. Read by the scorer, never by the agent |
| Rulebooks | `ASSESS_INSTRUCTIONS.md` (assess) and `ANALYZE_INSTRUCTIONS.md` (analyze), each followed by the shared `CONVENTIONS.md`; served verbatim (`clin_genomic_analysis_benchmark/instructions.py`) |
| Adapters | `adapters/claude_code/` (Claude Code on Vertex), `adapters/claude_code_vllm_gemma4/` (Claude Code with any Gemma 4 model on local vLLM), `adapters/claude_code_vertex_gemini/` (Claude Code with Gemini on Vertex Agent Platform), `adapters/claude_code_vertex_gemma4/` (Claude Code with Gemma 4 MaaS on Vertex Agent Platform), `adapters/codex_gpt/` (Codex CLI with configurable provider/model), `adapters/codex_vllm_gemma4/` (Codex with any Gemma 4 model on local vLLM), `adapters/opencode_vllm_gemma4/` (OpenCode with any Gemma 4 model on local vLLM), `adapters/opencode_azure_gpt/` (OpenCode with a GPT deployment on Azure OpenAI), `adapters/codex_vertex_gemma4_26b/` (Codex with Gemma 4 26B on Vertex Agent Platform), `adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/` and `adapters/codex_qwen_3.6_27B_GGUF_Unsloth_q4bitxl/` (Codex CLI against Unsloth Studio), `adapters/antigravity_gemini/` (Gemini through Antigravity CLI), `adapters/template/` to write your own |

### Keeping the answers away from the model

The assess model receives one prompt (the rulebook, then the question) through a
model API and has no tools, so it cannot reach any file. Its run records
`integrity.boundary: prompt_only`.

For analyze, the model-controlled CLI runs in a mandatory, fail-closed Linux `bubblewrap`
mount namespace. It sees only the current cohort at `/data/cohort` (read-only),
its dictionary under `/data/dictionary` (read-only), a per-question `/work`
directory (writable), an ephemeral home, and the required software runtime.
The repository, prior runs, scorecards, normal Claude/Codex histories, and gold
root are not mounted. Host absolute paths are replaced with these canonical
aliases before the prompt is built.

The gold question bank, computed gold answers, and review workbook additionally
remain outside the repository under **`$CLINGEN_GOLD_ROOT`** (default:
`../chatbpc/chatbpc_benchmark_gold`). This separation is defense in depth; the
filesystem namespace is the confidentiality boundary.

The repo's `questions/<cohort>.yaml` is a projection with the answers stripped
out—it is a different Pydantic model with no fields for classification,
component verdicts, or gold answers. Models see only public question text. The
trusted harness reads the gold bank's classifications to choose which questions
the analyze task runs (the agent is never told more than that its question is
fully specified), and the scorer reads the gold bank to score.

Every analyze run performs a namespace preflight and scans agent artifacts afterward.
`manifest.json` and both scorecards record `integrity.status`. Automated
postflight-clean runs are `valid`. A run accepted under a narrow, recorded
exception is `adjudicated_for_scoring`; its original findings and adjudication
remain embedded in the scorecard. `quarantined` or legacy `unaudited` runs must
not be used for model comparisons. Network access remains available because the CLI must
reach its configured endpoint, but host `/tmp`, Unix sockets, and the host
filesystem outside the explicit mounts are absent.

## How scoring works

Assess and analyze are separate tasks, run separately and usually with different models. Each run gets its own scorecard, and each task's score is a fraction in [0, 1]. `clingen-bench combine` weights one scored assess run and one scored analyze run into the headline:

```
SCORE = ½ · assess + ½ · analyze
```

Weights live in `scoring_configs/default.yaml` if you want something other than halves. `combine` refuses to mix runs that cover different cohorts, were scored against different gold banks, used different versions of `CONVENTIONS.md`, or left questions out (override with `--allow-mismatch`, which the combined scorecard records). A combined scorecard looks like this:

```
- SCORE: 68.2% — weighted mean of the two tasks

| task    | score | weight |
| assess  | 71.4% |    50% |
| analyze | 65.0% |    50% |
```

followed by a per-cohort table. Each run's own scorecard has the detail: for assess, a per-component table (sensitivity, specificity, balanced accuracy, accuracy, missing verdicts); for analyze, points and bands per cohort, category, and answer type; and for both, one row per question.

### 1. Assess

The model judges four components of the question and says, for each, whether it **needs clarification** before one analysis follows from it. The prompt is `ASSESS_INSTRUCTIONS.md`, then `CONVENTIONS.md`, then the question and the cohort's cancer type. Nothing else: no data, no data dictionary, no tools. Clarity is judged from the researcher's words and the rulebook's conventions, not from what any dataset records or how it defines a term.

| component | covers |
|---|---|
| `denominator` | who or what is analysed: the cancer and histology, stage and disease burden (at diagnosis or later; which stage groups), biomarker definitions and testing timing, qualifying treatment and line of therapy, patient characteristics, time windows, unit of analysis |
| `outcome` | what is measured, and missing values; for time-to-event outcomes, the index date, how events are ascertained, censoring and competing events |
| `covariates` | exposures, comparison groups, stratifiers, adjustment covariates — each defined so every analysed unit falls in exactly one category or takes one value, including units with unknown values |
| `analysis_plan` | the statistical estimand, the model or test, the variables in any multivariable model, left truncation, exposures ascertained after the index date, landmarks |

The model returns one verdict per component with a short rationale:

```json
{
  "denominator":   {"needs_clarification": false, "rationale": "..."},
  "outcome":       {"needs_clarification": true,  "rationale": "..."},
  "covariates":    {"needs_clarification": false, "rationale": "..."},
  "analysis_plan": {"needs_clarification": true,  "rationale": "..."}
}
```

A question is **ambiguous** if any component needs clarification and **unambiguous** if none does. `ASSESS_INSTRUCTIONS.md` catalogs what each component must pin down to be clear.

Every question in the bank is assessed. Scoring is exact and deterministic — no semantic matcher, no LLM call. Each verdict is compared with the gold verdict for that component; the rationale is recorded for auditing but never scored. The assess score is the **mean over the four components of balanced accuracy**: the average of the hit rate on gold "needs clarification" verdicts and the hit rate on gold "clear" verdicts. Most gold verdicts are "clear", so plain accuracy would reward a model that calls every component clear; balanced accuracy scores that strategy (and its opposite) at 50%. A call that never returns a valid reply (after its retries) counts as wrong on every component. Where a slice of the bank has only one gold class for a component (e.g. a cohort where no question leaves covariates unclear), that component's balanced accuracy is the one defined rate, and the scorecard marks it single-class.

The scorecard also reports question-level ambiguous/unambiguous accuracy derived from the verdicts. It is diagnostic only: it is implied by the component verdicts and is not part of the score.

### 2. Analyze

The agent runs on every **gold-unambiguous** question, whatever any assess run concluded. The system prompt is `ANALYZE_INSTRUCTIONS.md` then `CONVENTIONS.md`; the agent computes the answer from the cohort files and returns typed JSON — an `answer_type` plus an `answer` object, described in `ANALYZE_INSTRUCTIONS.md`.

Scoring compares the agent's **point estimate** against the gold and lands in one of three bands:

| band | points | meaning |
|---|---:|---|
| ACCURATE | 2 | within 5% of gold (or the right bucket / exact category) |
| MINOR | 1 | 5–15% off (or one bucket away) |
| MAJOR | 0 | more than 15% off, or wrong bucket, wrong category, wrong type |

The analyze score is points earned over 2 points per gold-unambiguous question in the run; a question whose agent call failed scores 0. A question relabelled ambiguous after the run is excluded, and a gold-unambiguous question the run did not include is listed in the scorecard rather than scored.

How "off" is measured depends on the answer type:

| `answer_type` | comparison |
|---|---|
| `count` | relative difference, `abs(agent − gold) / abs(gold)` |
| `proportion` | relative difference by default; can be switched to absolute percentage points per question |
| `median_with_ci` | relative difference on the median |
| `hazard_ratio_with_ci`, `odds_ratio_with_ci` | relative difference **on the log scale**, `abs(log a − log g) / abs(log g)` — so an HR of 2.0 against a gold of 1.0 is not treated as "100% off" the way a raw ratio would be |
| `pvalue` | bucketed into `<0.001`, `<0.01`, `<0.05`, `≥0.05`. Same bucket = ACCURATE, one bucket away = MINOR, further = MAJOR |
| `categorical` | exact string match after lowercasing and trimming. ACCURATE or MAJOR — no partial credit |
| `categorical_distribution` | every category compared individually; the **worst-fitting** category sets the band, so a wrong small category cannot hide behind correct large ones. The agent's category keys must match the gold's exactly |

Some things score zero outright, regardless of how close the number looks:

- **Wrong `answer_type`.** A correct median submitted as a `count` is a MAJOR.
- **A missing or unparseable value.**
- **Gold is exactly 0 and the agent is not** (both zero is ACCURATE).
- **A non-positive hazard or odds ratio**, which cannot be log-transformed.

Two details worth knowing:

**Confidence intervals are not scored.** The contract asks for `ci_low` / `ci_high` on medians, hazard ratios, odds ratios, and proportions, and the gold records them, but the band is set by the point estimate alone. The CIs are there for auditing a suspicious answer, not for grading.

**P-values are graded coarsely on purpose.** Bucketing to `<0.001 / <0.01 / <0.05 / ≥0.05` rewards reaching the same inferential conclusion rather than reproducing a float, which would otherwise punish immaterial differences in tie handling or solver defaults.

Thresholds are configurable per answer type and per question in `scoring_configs/default.yaml`.

#### Unanswerable questions

Occasionally a question is perfectly well specified but the data cannot identify the estimand — for example, a Cox interaction where one cell of the 2×2 is nearly empty and the model separates.

These stay unambiguous (every component clear), so they are in the analyze task, and the gold carries `unanswerable: true` along with the cell counts and an explanation. An agent that recognises the problem and flags `unanswerable: true` itself is scored ACCURATE (2 points). Any other answer is scored MAJOR (0 points), including a numeric value that matches the gold placeholder with an omitted or false `unanswerable` flag. The agent's `answer_type` is not checked for these questions: agents are not told the expected type, and with no estimate to compare the type carries no signal.

**Do not relabel these as needing clarification.** The question is fine — the data are insufficient, which is a different finding and one the benchmark deliberately tests for.

## Cohorts

`bladder_1.2`, `breast_1.2`, `crc_2.0_public`, `nsclc_2.0_public`, `panc_1.2`, `prostate_1.2` — one read-only directory per cohort, not in git. `CLINGEN_DATA_ROOT` has to be set to the directory that contains them. Questions are scored per cohort as well as overall.

## The question bank and how to change it

The source of truth is the **review workbook**, `$CLINGEN_GOLD_ROOT/bpc_benchmark_review_6-19-26.xlsx`: one row per question, holding the classification, question text, the four component verdict columns (`denominator_needs_clarification`, `outcome_needs_clarification`, `covariates_needs_clarification`, `analysis_plan_needs_clarification`, each Y/N) with a `<component>_notes` column beside each, analysis plan, expected answer type, gold answer, gold script, and the per-round review columns that record how the bank got here. The retired concept-menu columns (`disambiguation_concepts`, `disambiguation_concept_ids`) stay as history and are carried into the gold bank unscored. In this workspace the default resolves to `~/Partners HealthCare Dropbox/Kenneth Kehl/chatbpc/chatbpc_benchmark_gold/bpc_benchmark_review_6-19-26.xlsx`.

A row with all four verdicts filled is reviewed under the four-component rubric; the `classification` column must agree with them (ambiguous exactly when some component needs clarification). A row with none filled is unreviewed: it syncs without component gold, and scoring refuses it. A partly filled row is an error.

The harness never reads the workbook. After any edit, regenerate both YAML banks:

```bash
.venv/bin/python scripts/sync_yaml_from_review.py
```

That validates the workbook against the `Question` schema and writes the gold bank to `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` and the stripped public bank to `questions/<cohort>.yaml`, then prints how many questions have component gold. **Skip it and the agent and scorer are working from a stale bank.**

## Quickstart

```bash
uv sync
command -v bwrap             # required; evaluation fails closed without it
cp .env.example .env   # Vertex (ANTHROPIC_VERTEX_PROJECT_ID, credentials) + Azure OpenAI
export CLINGEN_DATA_ROOT=/path/to/cohort/data   # required: directory containing the cohort folders

# Make sure the YAML banks match the workbook
.venv/bin/python scripts/sync_yaml_from_review.py

# ---- assess: one prompt per question, no tools ----------------------------
# --model is <provider>:<model>, with providers vertex-anthropic, vertex-openai
# (Gemini, Gemma, and other Vertex MaaS models), vertex-responses (partner
# models Vertex serves only on its Responses endpoint, such as
# meta/muse-ember-1.0), azure-openai, and
# openai-compatible (a local vLLM or Unsloth server; pass --base-url). Extra
# request fields go in --params as JSON. Each question is called up to
# --max-attempts times until its reply validates; the run is then scored.
uv run clingen-bench assess \
  --model vertex-anthropic:claude-opus-5-5 \
  --params '{"thinking": {"type": "adaptive"}}' \
  --cohort bladder_1.2 \
  --max-parallel 8

uv run clingen-bench assess \
  --model openai-compatible:google/gemma-4-26b-it \
  --base-url http://127.0.0.1:8000/v1 \
  --cohort bladder_1.2

# ---- analyze: an agent computes every gold-unambiguous question -------------
# Each question spawns an agent that may run Python against the cohort files,
# so a full run takes real time and real spend. Each question is attempted up
# to 3 times with exponential backoff; override with --agent-max-attempts and
# --agent-retry-base-seconds. Automatic repair uses up to 10 attempts per failed
# question and up to 10 passes; override with --repair-agent-max-attempts and
# --repair-max-passes.
uv run clingen-bench analyze \
  --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code \
  --cohort bladder_1.2 \
  --max-parallel 4

# analyze automatically checks for score-relevant technical failures afterward.
# When any remain after the per-question attempts, it repairs them in a copied
# run, rescoring that copy and printing its path as the final artifact. Use
# --no-retry-failures only when an intentionally raw run is desired.

# ---- scoring -------------------------------------------------------------------
# Score a run locally (either task). Scoring makes no model or network calls.
uv run clingen-bench score --run claude_code/<new_run_id>

# The headline: weight one scored assess run and one scored analyze run.
uv run clingen-bench combine \
  --assess-run assess_vertex-anthropic-claude-opus-5-5/<run_id> \
  --analyze-run claude_code/<run_id>

# ---- salvaging failures ----------------------------------------------------------
# retry-failures works on either task. It first lists only technical failures
# that can affect scoring. For an assess run it re-calls the failed questions
# with the recorded model settings (and refuses if the rulebook has changed).
uv run clingen-bench retry-failures \
  --run claude_code/<run_id> \
  --dry-run

# For an analyze run it retries in fresh isolated scratch directories. Repair uses
# at most 2 concurrent calls by default and continues over successively repaired
# copies until complete, a pass makes no progress, or --max-repair-passes is
# reached. Every pass records its hashes, effective timeouts, and provenance.
# Gold-bearing scorecards are never copied into the repair tree before retries.
# When repair finishes, the original and intermediate copies are moved under
# runs/<agent>/pre_repair/; only the final rescored copy remains directly under
# runs/<agent>/. Lineage references and source hashes are updated after the move.
# The original model, provider, effort, Vertex project, region, and timeout are
# restored from its manifest unless an explicit repair-only timeout override is
# supplied.
uv run clingen-bench retry-failures \
  --run claude_code/<run_id> \
  --max-parallel 2 \
  --max-repair-passes 10 \
  --analyze-timeout 3600

# One question, for a smoke test
uv run clingen-bench analyze --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code --question bladder_1.2-Q6f9dd68e

# Look at a cohort's data dictionary and file inventory
uv run clingen-bench inspect --cohort bladder_1.2
```

`CLINGEN_CLAUDE_MODEL` (default `claude-opus-4-8`) sets the reference analyze
agent's model. `CLINGEN_CLAUDE_EFFORT` optionally pins Claude Code's effort level by
passing `--effort` (for example, `xhigh`). Claude Haiku 4.5 does not support
configurable effort, so leave the variable unset for that model. Neither setting
has any role in scoring.

Every analyze run's `manifest.json` includes a non-secret `agent_provenance` block. The
Claude Code, Codex, OpenCode, and Antigravity adapters record the effective model,
provider, effort level, and the source of each resolved value; cloud-backed runs
also record the project and region when available. Codex values are resolved
from explicit `CODEX_*` overrides, the selected profile, or the base user config
in precedence order. Antigravity requires an explicit `AGY_MODEL` and records
either `AGY_EFFORT` or the model's High/Medium/Low suffix. The same provenance
appears at the top of generated scorecards. An assess run's manifest records
`model_provenance` instead (provider, model, request parameters, a redacted
base URL) and the SHA-256 of the rulebook files its prompt was built from. API
keys and credential paths are never included.

`generate-questions` and `compute-gold` build a bank and its gold answers with an LLM. They are how the bank started, but it is now curated by hand in the workbook, so day to day you want `sync_yaml_from_review.py` instead.

## Writing an agent

Agents run only the analyze task. They are **CLI executables**, called once per gold-unambiguous question:

```bash
$ <your_agent> --question-file question.json --output result.json
```

The harness contract (`agent/contract.py`, version 5) remains model- and
framework-agnostic, but a coding-agent adapter must put every model-controlled
subprocess through `agent.isolation.sandboxed_agent_command` and be added to the
sandboxed-adapter registry. Unregistered adapters fail closed. Claude Code, all
Codex adapters, OpenCode, and Antigravity are registered; the template adapter
is intentionally not certified for benchmark runs.

The reference Claude adapter builds a system prompt from
`instructions.analyze_rulebook()` (`ANALYZE_INSTRUCTIONS.md` then
`CONVENTIONS.md`), then launches `claude` inside the outer namespace with
its normal narrow tool allowlist. Codex also uses its CLI's internal sandbox as
defense in depth. Antigravity relies on the outer boundary because its Linux
`nsjail` cannot execute correctly when nested inside bubblewrap.

Start from `adapters/template/`. The assess task needs no adapter: add a model
API to `assess/backends.py` if none of the five providers reaches it.

## Repo layout

- `clin_genomic_analysis_benchmark/` — the Python package: CLI `clingen-bench`, evaluation pipeline, scoring
- `clin_genomic_analysis_benchmark/components.py` — the four assessed components (IDs, labels, descriptions)
- `clin_genomic_analysis_benchmark/assess/` — the assess task: prompt, model backends, runner
- `clin_genomic_analysis_benchmark/agent/` — the analyze task: agent contract, sandbox, orchestrator, repair
- `ASSESS_INSTRUCTIONS.md`, `ANALYZE_INSTRUCTIONS.md`, `CONVENTIONS.md` — the rulebooks; `CONVENTIONS.md` is the authoritative statement of the conventions below
- `questions/<cohort>.yaml` — public, answer-free question banks
- `adapters/` — agent adapters
- `scripts/sync_yaml_from_review.py` — workbook → both YAML banks
- `scoring_configs/default.yaml` — subtask weights, discrepancy thresholds, per-question overrides
- `runs/<run_name>/<run_id>/` — assess or analyze output and `scorecard.{md,json}`; `runs/combined/<id>/` — combined scorecards (not in git)

Under `$CLINGEN_GOLD_ROOT`, away from the agent:

- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, source of truth
- `questions/<cohort>.yaml` — the gold banks the scorer reads
- `gold_standard/<cohort>/` — a Python script and `result.json` per unambiguous question. The live set is whatever the workbook's `gold_script` column points at; the folders also hold leftovers from removed or reclassified questions
- `bpc_benchmark_for_msk_6-20-26/` — a shareable snapshot that intentionally bundles gold

## Conventions the bank assumes

`CONVENTIONS.md` is authoritative, and `ASSESS_INSTRUCTIONS.md` says how gaps are judged. The ones an operator most needs to know:

- **Clarity is judged from the words, not the data.** A component needs clarification when the question leaves a choice open and no convention settles it — even if one dataset (BPC included) could support only one reading, and even if every reading would give the same result. A question settles a choice by stating it, by a convention, or by adopting a named definition: a published standard ("AJCC 8th edition", "RECIST 1.1"), a named endpoint the conventions define (PFS-I, PFS-M, PFS-I-or-M, PFS-I-and-M), or the data source's own definition by explicit reference. A bare "Stage IV" leaves open whether it means stage at diagnosis.
- **Key index cancer.** Unless a question says otherwise, each patient is represented by one *key index cancer* — their earliest-diagnosed sequenced cancer of the cohort's type that meets the question's cancer-level criteria. A later, separate primary that meets them counts, unless the question excludes patients with prior primary cancers. Question stems generally do not contain the word "index"; "patients with `<cohort>` cancer" already means this. Multiple index cancers, and which specimens and regimens belong to which, are resolved by this convention rather than flagged.
- **Genomic-specimen scope.** Genomic facts come from specimens of the cohort's cancer. Patient-level genomic questions use the key index cancer's specimens; sample-level counts use all of that patient's cohort-cancer specimens.
- **Unknown values.** Every characteristic except date of birth, sex, diagnosis dates, and vital status is presumed able to be unknown, so a question that groups or measures by it must say how unknowns are handled. Eligibility criteria apply only to units known to meet them.
- **Defaults that still settle a component.** Outcomes of treated cohorts are measured from regimen start unless the question says otherwise. A hazard ratio means an unadjusted lifelines Cox model unless the question names covariates, says "adjusted", or specifies another model structure. Overall survival and the named PFS endpoints carry their own event and censoring definitions. A month is 30.4375 days.
- **BPC mappings live in `ANALYZE_INSTRUCTIONS.md`.** For example, stage at diagnosis is `stage_dx`, a distant metastasis after a Stage I–III diagnosis is `dmets_stage_i_iii` (recorded only for the first BPC Project Cancer), and lines of therapy are numbered with `regimen_number_within_cancer`. Gold scripts must follow these mappings, because the assess task no longer flags choices that only a dataset forces.
- **`dmets_<site>`** is an ever-indicator; at-diagnosis questions use `dx_to_dmets_<site>_days <= 30`. **cBioPortal CNA** values run −2 to 2, where 2 is high-level amplification and −2 a deep deletion.
- **`analysis_plan_summary`** in the workbook is a self-contained prose method spec for each unambiguous question.

## Pointers

- `README.txt` — plain-language orientation for a non-developer
- `ASSESS_INSTRUCTIONS.md`, `ANALYZE_INSTRUCTIONS.md`, `CONVENTIONS.md` — the rules each task's model follows
- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, including the review columns that record its revision history
