# clin-genomic-analysis-benchmark

A benchmark for AI agents and coding assistants on **translational clinical cancer-data analysis**, built on AACR Project GENIE BPC (Biopharma Collaborative) cohort data.

Real analyses of clinico-genomic data fail in two different ways. Sometimes the question is clear and the agent computes the wrong number. More often the question *sounds* clear but isn't — "how did patients on first-line chemotherapy do?" hides a dozen choices about what counts as first line, what counts as chemotherapy, and what "did" means. A competent analyst notices and asks. This benchmark measures both: whether an agent can tell which parts of a question are underspecified — who is analysed, what is measured, which variables are compared or adjusted for, and how the estimate is computed — and whether it gets the number right when the question is sound.

> Not a developer? **`README.txt`** is a plain-language tour of the task, the review workbook, and the gold answers. This file is the operator's guide.

## Status

All six cohorts are wired end to end. The reference adapter drives Claude Code on `claude-opus-4-8` via Vertex.

| Component | Status |
|---|---|
| Question bank | **205 questions across six cohorts, being re-reviewed under the four-component rubric** (started 2026-10-25). Every question is reviewed and may be edited; component gold labels are pending, and scoring refuses a question until it has them. Curated in `bpc_benchmark_review_6-19-26.xlsx` under `$CLINGEN_GOLD_ROOT` |
| Gold answers | Every unambiguous question has a computed, re-runnable gold answer. Answers are recomputed as questions are edited during the re-review. Questions that are well specified but not estimable are marked `unanswerable` (see below) |
| Agent-facing bank | `questions/<cohort>.yaml` — `id`, `category`, `text` only. The agent under test sees the `id` and `text`; `category` stays harness-side for per-category reporting |
| Gold bank | `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` — answers, per-component verdicts with reviewer notes, classifications, and legacy concept prose kept as unscored history. Read by the scorer, never by the agent |
| Agent guidance | `AGENT_INSTRUCTIONS.md` — served verbatim to the agent by the reference adapter |
| Adapters | `adapters/claude_code/` (Claude Code on Vertex), `adapters/claude_code_vllm_gemma4/` (Claude Code with any Gemma 4 model on local vLLM), `adapters/claude_code_vertex_gemini/` (Claude Code with Gemini on Vertex Agent Platform), `adapters/claude_code_vertex_gemma4/` (Claude Code with Gemma 4 MaaS on Vertex Agent Platform), `adapters/codex_gpt/` (Codex CLI with configurable provider/model), `adapters/codex_vllm_gemma4/` (Codex with any Gemma 4 model on local vLLM), `adapters/opencode_vllm_gemma4/` (OpenCode with any Gemma 4 model on local vLLM), `adapters/opencode_azure_gpt/` (OpenCode with a GPT deployment on Azure OpenAI), `adapters/codex_vertex_gemma4_26b/` (Codex with Gemma 4 26B on Vertex Agent Platform), `adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/` and `adapters/codex_qwen_3.6_27B_GGUF_Unsloth_q4bitxl/` (Codex CLI against Unsloth Studio), `adapters/antigravity_gemini/` (Gemini through Antigravity CLI), `adapters/template/` to write your own |

### Keeping the answers away from the agent

The model-controlled CLI runs in a mandatory, fail-closed Linux `bubblewrap`
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
component verdicts, or gold answers. Evaluation reads the public bank; only the trusted
harness/scorer reads the gold bank.

Every run performs a namespace preflight and scans agent artifacts afterward.
`manifest.json` and both scorecards record `integrity.status`. Automated
postflight-clean runs are `valid`. A run accepted under a narrow, recorded
exception is `adjudicated_for_scoring`; its original findings and adjudication
remain embedded in the scorecard. `quarantined` or legacy `unaudited` runs must
not be used for model comparisons. Network access remains available because the CLI must
reach its configured endpoint, but host `/tmp`, Unix sockets, and the host
filesystem outside the explicit mounts are absent.

## How scoring works

Every question is served to the agent in up to two stages: **assess**, then **analyze** if the agent judged the question fully specified.

The headline score is the weighted mean of the two subtasks, each first expressed as a fraction in [0, 1]:

```
SCORE = ½ · assess + ½ · analyze
```

Weights live in `scoring_configs/default.yaml` if you want something other than halves. A scorecard looks like this:

```
- SCORE: 68.2% — weighted mean of the two subtasks

| subtask | score | weight | basis                                                  |
| assess  | 71.4% |    50% | mean of per-component balanced accuracy                |
| analyze | 65.0% |    50% | 130.0 / 200.0 pts over gold-unambiguous questions      |
```

followed by a per-component table (sensitivity, specificity, balanced accuracy, accuracy, missing verdicts), per-cohort and per-category breakdowns, and one row per question.

**Denominators come from the gold, not from the route the agent took.** If an agent flags a component of a question whose gold is fully specified, it never reaches analyze and still owes those analysis points. Misrouting costs twice, which is the intent.

### 1. Assess

The agent judges four components of the question and says, for each, whether it **needs clarification** before one analysis pipeline follows from it, given the BPC data dictionary and the rulebook's conventions. This is judged from the wording, not from what the cohort data contain:

| component | covers |
|---|---|
| `denominator` | who or what is analysed: cancer type and histology, disease burden (de novo vs recurrent), line of therapy, qualifying treatment, genomic-testing timing (genomic questions), unit of analysis |
| `outcome` | what is measured, and missing values; for time-to-event outcomes, the index date, how progression or response is ascertained, censoring and competing events |
| `covariates` | exposures, comparison groups, stratifiers, adjustment covariates — each defined so every analysed record falls in exactly one category or takes one value, including records with unknown values |
| `analysis_plan` | the statistical estimand, the model architecture, the variables in any multivariable model, left truncation, exposures ascertained after the index date |

The agent returns one verdict per component with a short rationale:

```json
{
  "denominator":   {"needs_clarification": false, "rationale": "..."},
  "outcome":       {"needs_clarification": true,  "rationale": "..."},
  "covariates":    {"needs_clarification": false, "rationale": "..."},
  "analysis_plan": {"needs_clarification": true,  "rationale": "..."}
}
```

A question is **ambiguous** if any component needs clarification and **unambiguous** if none does. `AGENT_INSTRUCTIONS.md` gives examples of what each component must pin down to be clear.

Scoring is exact and deterministic — no semantic matcher, no LLM call. Each verdict is compared with the gold verdict for that component; the rationale is recorded for auditing but never scored. The assess score is the **mean over the four components of balanced accuracy**: the average of the hit rate on gold "needs clarification" verdicts and the hit rate on gold "clear" verdicts. Most gold verdicts are "clear", so plain accuracy would reward an agent that calls every component clear; balanced accuracy scores that strategy (and its opposite) at 50%. A failed or invalid assess stage counts as wrong on every component. Where a slice of the bank has only one gold class for a component (e.g. a cohort where no question leaves covariates unclear), that component's balanced accuracy is the one defined rate, and the scorecard marks it single-class.

The scorecard also reports question-level ambiguous/unambiguous accuracy derived from the verdicts. It is diagnostic only: it is implied by the component verdicts and is not part of the headline.

### 2. Analyze

Only asked when the agent judged every component clear. The agent computes the answer from the cohort files and returns typed JSON — an `answer_type` plus an `answer` object, described in `AGENT_INSTRUCTIONS.md`.

Scoring compares the agent's **point estimate** against the gold and lands in one of three bands:

| band | points | meaning |
|---|---:|---|
| ACCURATE | 2 | within 5% of gold (or the right bucket / exact category) |
| MINOR | 1 | 5–15% off (or one bucket away) |
| MAJOR | 0 | more than 15% off, or wrong bucket, wrong category, wrong type |

The analyze score is points earned over 2 points per gold-unambiguous question.

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

These stay unambiguous (every component clear), and the gold carries `unanswerable: true` along with the cell counts and an explanation. An agent that recognises the problem and flags `unanswerable: true` itself is scored ACCURATE (2 points). Any other answer is scored MAJOR (0 points), including a numeric value that matches the gold placeholder with an omitted or false `unanswerable` flag. The agent's `answer_type` is not checked for these questions: agents are not told the expected type, and with no estimate to compare the type carries no signal.

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

# Run an agent over one cohort. Each stage spawns an agent that may run Python
# against the cohort files, so a full run takes real time and real spend.
# Initial-evaluation stages are attempted up to 3 times with exponential
# backoff; override with --agent-max-attempts and --agent-retry-base-seconds.
# Automatic repair uses up to 10 attempts per failed stage and up to 10 passes;
# override with --repair-agent-max-attempts and --repair-max-passes.
uv run clingen-bench eval \
  --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code \
  --cohort bladder_1.2 \
  --max-parallel 4

# eval automatically checks for score-relevant technical failures afterward.
# When any remain after the per-stage attempts, it repairs them in a copied run,
# rescoring that copy and printing its path as the final artifact. Use
# --no-retry-failures only when an intentionally raw run is desired.

# Score it locally. Scoring makes no model or network calls.
uv run clingen-bench score --run claude_code/<new_run_id>

# Salvage a completed run without rerunning successful questions. This first
# lists only technical failures that can affect scoring; wrong-route downstream
# failures are intentionally excluded.
uv run clingen-bench retry-failures \
  --run claude_code/<run_id> \
  --dry-run

# Retry the selected stages in fresh isolated scratch directories. Repair uses
# at most 2 concurrent calls by default and continues over successively repaired
# copies until complete, a pass makes no progress, or --max-repair-passes is
# reached. Every pass records its hashes, effective timeouts, and provenance.
# Gold-bearing scorecards are never copied into the repair tree before retries.
# When repair finishes, the original and intermediate copies are moved under
# runs/<agent>/pre_repair/; only the final rescored copy remains directly under
# runs/<agent>/. Lineage references and source hashes are updated after the move.
# The original model, provider, effort, Vertex project, region, and stage
# timeouts are restored from its manifest unless an explicit repair-only timeout
# override is supplied.
uv run clingen-bench retry-failures \
  --run claude_code/<run_id> \
  --max-parallel 2 \
  --max-repair-passes 10 \
  --assess-timeout 3600

# One question, for a smoke test
uv run clingen-bench eval --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code --question bladder_1.2-Q6f9dd68e

# Look at a cohort's data dictionary and file inventory
uv run clingen-bench inspect --cohort bladder_1.2
```

`CLINGEN_CLAUDE_MODEL` (default `claude-opus-4-8`) sets the reference agent
model. `CLINGEN_CLAUDE_EFFORT` optionally pins Claude Code's effort level by
passing `--effort` (for example, `xhigh`). Claude Haiku 4.5 does not support
configurable effort, so leave the variable unset for that model. Neither setting
has any role in scoring.

Every run's `manifest.json` includes a non-secret `agent_provenance` block. The
Claude Code, Codex, OpenCode, and Antigravity adapters record the effective model,
provider, effort level, and the source of each resolved value; cloud-backed runs
also record the project and region when available. Codex values are resolved
from explicit `CODEX_*` overrides, the selected profile, or the base user config
in precedence order. Antigravity requires an explicit `AGY_MODEL` and records
either `AGY_EFFORT` or the model's High/Medium/Low suffix. The same provenance
appears at the top of generated scorecards. API keys and credential paths are
never included.

`generate-questions` and `compute-gold` build a bank and its gold answers with an LLM. They are how the bank started, but it is now curated by hand in the workbook, so day to day you want `sync_yaml_from_review.py` instead.

## Writing an agent

Agents are **CLI executables**, called once per (question, stage), where the stage is `assess` or `analyze`:

```bash
$ <your_agent> --question-file question.json --output result.json
```

The harness contract remains model- and framework-agnostic, but a coding-agent
adapter must put every model-controlled subprocess through
`agent.isolation.sandboxed_agent_command` and be added to the sandboxed-adapter
registry. Unregistered adapters fail closed. Claude Code, all Codex adapters,
OpenCode, and Antigravity are registered; the template adapter is intentionally not
certified for benchmark runs.

The reference Claude adapter builds a system prompt from
`AGENT_INSTRUCTIONS.md`, then launches `claude` inside the outer namespace with
its normal narrow tool allowlist. Codex also uses its CLI's internal sandbox as
defense in depth. Antigravity relies on the outer boundary because its Linux
`nsjail` cannot execute correctly when nested inside bubblewrap.

Start from `adapters/template/`.

## Repo layout

- `clin_genomic_analysis_benchmark/` — the Python package: CLI `clingen-bench`, evaluation pipeline, scoring
- `clin_genomic_analysis_benchmark/components.py` — the four assessed components (IDs, labels, descriptions)
- `AGENT_INSTRUCTIONS.md` — the rules the agent under test follows; the authoritative statement of the conventions below
- `questions/<cohort>.yaml` — public, answer-free question banks
- `adapters/` — agent adapters
- `scripts/sync_yaml_from_review.py` — workbook → both YAML banks
- `scoring_configs/default.yaml` — subtask weights, discrepancy thresholds, per-question overrides
- `runs/<agent>/<run_id>/` — evaluation output and `scorecard.{md,json}` (not in git)

Under `$CLINGEN_GOLD_ROOT`, away from the agent:

- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, source of truth
- `questions/<cohort>.yaml` — the gold banks the scorer reads
- `gold_standard/<cohort>/` — a Python script and `result.json` per unambiguous question. The live set is whatever the workbook's `gold_script` column points at; the folders also hold leftovers from removed or reclassified questions
- `bpc_benchmark_for_msk_6-20-26/` — a shareable snapshot that intentionally bundles gold

## Conventions the bank assumes

`AGENT_INSTRUCTIONS.md` is authoritative. The ones an operator most needs to know:

- **Key index cancer.** Unless a question says otherwise, each patient is represented by one *key index cancer* — their earliest-diagnosed genomically profiled index cancer that meets the question's cancer-level criteria. A later, separate primary that meets them counts, unless the question excludes patients with prior primary cancers. Question stems generally do not contain the word "index"; "patients with `<cohort>` cancer" already means this. Multiple index cancers, and which specimens and regimens belong to which, are resolved by this convention rather than flagged.
- **Genomic-specimen scope.** Genomic facts come from specimens of the cohort's cancer. Patient-level genomic questions use the key index cancer's specimen; sample-level counts use all of that patient's cohort-cancer specimens.
- **Patient-level attributes** — sex, race, ethnicity, vital status — come from `patient_level_dataset.csv`, and anatomic scope does not apply to them.
- **What counts as a gap.** A component needs clarification when the question leaves a choice open and no convention settles it. This is judged from the wording, not the data: a choice left open needs clarification even if every reading would give the same result in this cohort. The rulebook lists, for each component, example concepts a question often has to pin down — whether a stage term means stage at diagnosis; whether "advanced" or "metastatic" includes recurrent disease; the disease state of a line of therapy; the index date of a time-to-event outcome; how progression or response is ascertained (PFS-I, PFS-M, PFS-I-and-M, PFS-I-or-M); left truncation; the estimand; the model architecture; histology scope; a covariate's coding; missing values — and says the lists are not exhaustive.
- **Defaults that still settle a component.** Outcomes of treated cohorts are measured from regimen start unless the question says otherwise. A hazard ratio means an unadjusted lifelines Cox model unless the question names covariates, says "adjusted", or specifies another model structure. Canonical BPC endpoint variables carry their own event and censoring definitions.
- **`dmets_<site>`** is an ever-indicator; at-diagnosis questions use `dx_to_dmets_<site>_days <= 30`. **cBioPortal CNA** values run −2 to 2, where 2 is high-level amplification and −2 a deep deletion.
- **`analysis_plan_summary`** in the workbook is a self-contained prose method spec for each unambiguous question.

## Pointers

- `README.txt` — plain-language orientation for a non-developer
- `AGENT_INSTRUCTIONS.md` — the rules the agent under test follows
- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, including the review columns that record its revision history
