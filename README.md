# clin-genomic-analysis-benchmark

A benchmark for AI agents and coding assistants on **translational clinical cancer-data analysis**, built on AACR Project GENIE BPC (Biopharma Collaborative) cohort data.

Real analyses of clinico-genomic data fail in two different ways. Sometimes the question is clear and the agent computes the wrong number. More often the question *sounds* clear but isn't — "how did patients on first-line chemotherapy do?" hides a dozen choices about what counts as first line, what counts as chemotherapy, and what "did" means. A competent analyst notices and asks. This benchmark measures both: whether an agent can tell those two situations apart, what it asks for when a question is underspecified, and whether it gets the number right when the question is sound.

> Not a developer? **`README.txt`** is a plain-language tour of the task, the review workbook, and the gold answers. This file is the operator's guide.

## Status

All six cohorts are wired end to end. The reference adapter drives Claude Code on `claude-opus-4-8` via Vertex.

| Component | Status |
|---|---|
| Question bank | **211 questions — 96 unambiguous, 115 ambiguous**, after four rounds of oncologist review. Curated in `bpc_benchmark_review_6-19-26.xlsx` under `$CLINGEN_GOLD_ROOT` |
| Gold answers | 96 / 96 unambiguous questions have a computed, re-runnable gold answer; 1 is marked `unanswerable` (see below) |
| Agent-facing bank | `questions/<cohort>.yaml` — `id`, `category`, `text` only. This is everything the agent under test ever sees |
| Gold bank | `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` — answers, canonical concept IDs, classifications, and audit prose. Read by the scorer, never by the agent |
| Agent guidance | `AGENT_INSTRUCTIONS.md` — served verbatim to the agent by the reference adapter |
| Adapters | `adapters/claude_code/` (Claude Code on Vertex), `adapters/codex_gpt/` (Codex CLI with configurable provider/model), `adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/` (Codex CLI against Unsloth Studio), `adapters/antigravity_gemini/` (Gemini through Antigravity CLI), `adapters/template/` to write your own |

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
concepts, or gold answers. Evaluation reads the public bank; only the trusted
harness/scorer reads the gold bank.

Every run performs a namespace preflight and scans agent artifacts afterward.
`manifest.json` and both scorecards record `integrity.status`. Only `valid`
runs are certified; `quarantined` or legacy `unaudited` runs must not be used
for model comparisons. Network access remains available because the CLI must
reach its configured endpoint, but host `/tmp`, Unix sockets, and the host
filesystem outside the explicit mounts are absent.

## How scoring works

Every question is graded on **classify**, and then on **either disambiguate or analyze** depending on what the gold says the question is. No single question is graded on all three.

The headline score is the **equal-weighted mean of the three subtasks**, each first expressed as a fraction of its own available points:

```
SCORE = (classify% + disambiguate% + analyze%) / 3
```

It is deliberately not a sum of raw points. The three subtasks sit on different scales — 211 raw points for classify, 420 gold concept points for disambiguate before false-positive penalties, and 192 for analyze. Normalising each subtask first keeps the balance stable as the bank gains or loses questions. Weights live in `scoring_configs/default.yaml` if you want something other than thirds.

A scorecard looks like this:

```
- SCORE: 68.2% — weighted mean of the three subtasks

| subtask      | earned | possible | score | weight |
| classify     |  172.0 |    211.0 | 81.5% |    33% |
| disambiguate |  189.0 |    420.0 | 45.0% |    33% |
| analyze      |  150.0 |    192.0 | 78.1% |    33% |
```

**Denominators come from the gold, not from the route the agent took.** If an agent calls an ambiguous question unambiguous, it loses the classify point *and* still owes the disambiguation points it never attempted. Misrouting costs twice, which is the intent.

### 1. Classify

Is this question answerable as written, or does it leave a material analytic choice open? One point for the right label, zero for the wrong one. `AGENT_INSTRUCTIONS.md` sets out the standard in detail; the short version is that a question is unambiguous when a competent BPC analyst would reach for one column, one filter, and one method without asking.

### 2. Disambiguate

Only asked when the agent said the question was ambiguous. The task payload contains a fixed menu of canonical concept IDs (for example `OUTCOME_METRIC`, `LINE_OF_THERAPY`, and `PANEL_COVERAGE`), and the agent returns JSON such as:

```json
{"concept_ids": ["OUTCOME_METRIC", "TIME_ORIGIN"]}
```

Scoring is an exact set comparison—there is no semantic matcher and no LLM call:

| selection | default points |
|---|---:|
| ID is in the question's gold set | +1.00 |
| ID is not in the gold set | −0.25 |
| gold ID was omitted | 0 |

Per-question points are floored at zero and capped at the number of gold IDs. The false-positive penalty discourages selecting the whole menu: four incorrect selections cancel one correct selection. Both the reward and penalty are configurable in `scoring_configs/default.yaml`.

The scorecard JSON records the selected, correct, incorrect, and missed IDs for every question. The reviewed prose concepts remain in the out-of-repo workbook for auditability, while the adjacent `disambiguation_concept_ids` column is the machine-scored gold.

### 3. Analyze

Only asked when the agent said the question was unambiguous. The agent computes the answer from the cohort files and returns typed JSON — an `answer_type` plus an `answer` object, described in `AGENT_INSTRUCTIONS.md`.

Scoring compares the agent's **point estimate** against the gold and lands in one of three bands:

| band | points | meaning |
|---|---:|---|
| ACCURATE | 2 | within 5% of gold (or the right bucket / exact category) |
| MINOR | 1 | 5–15% off (or one bucket away) |
| MAJOR | 0 | more than 15% off, or wrong bucket, wrong category, wrong type |

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

Occasionally a question is perfectly well specified but the data cannot identify the estimand — for example `prostate_1.2-Qf17acd7c`, a Cox interaction between docetaxel and PTEN homozygous deletion where one cell of the 2×2 is nearly empty and the model separates.

These stay `classification: unambiguous`, and the gold carries `unanswerable: true` along with the cell counts and an explanation. An agent that recognises the problem and flags `unanswerable: true` itself is scored ACCURATE; an agent that returns a confident number is scored MAJOR.

**Do not reclassify these as ambiguous.** The question is fine — the data are insufficient, which is a different finding and one the benchmark deliberately tests for.

## Cohorts

`bladder_1.2`, `breast_1.2`, `crc_2.0_public`, `nsclc_2.0_public`, `panc_1.2`, `prostate_1.2` — read-only under `bpc_from_synapse/<cohort>/`, not in git. Questions are scored per cohort as well as overall.

## The question bank and how to change it

The source of truth is the **review workbook**, `$CLINGEN_GOLD_ROOT/bpc_benchmark_review_6-19-26.xlsx`: one row per question, holding the classification, question text, reviewed prose concepts, canonical disambiguation concept IDs, analysis plan, expected answer type, gold answer, gold script, and the per-round review columns that record how the bank got here. In this workspace the default resolves to `~/Partners HealthCare Dropbox/Kenneth Kehl/chatbpc/chatbpc_benchmark_gold/bpc_benchmark_review_6-19-26.xlsx`.

The harness never reads the workbook. After any edit, regenerate both YAML banks:

```bash
.venv/bin/python scripts/sync_yaml_from_review.py
```

That validates the workbook against the `Question` schema and writes the gold bank to `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` and the stripped public bank to `questions/<cohort>.yaml`. **Skip it and the agent and scorer are working from a stale bank.**

For an older workbook that has reviewed prose concepts but no canonical-ID column, run `scripts/migrate_workbook_concepts.py` once. It adds `disambiguation_concept_ids`, creates a `concept_menu` sheet, and preserves a `.pre_rules_backup.xlsx` copy before saving.

## Quickstart

```bash
uv sync
command -v bwrap             # required; evaluation fails closed without it
cp .env.example .env   # Vertex (ANTHROPIC_VERTEX_PROJECT_ID, credentials) + Azure OpenAI

# Make sure the YAML banks match the workbook
.venv/bin/python scripts/sync_yaml_from_review.py

# Run an agent over one cohort. Each analyze question spawns an agent that runs
# Python against the cohort files, so a full run takes real time and real spend.
# Failed stages are attempted up to 3 times with exponential backoff; override
# with --agent-max-attempts and --agent-retry-base-seconds.
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

# Retry the selected stages (up to 3 attempts each) in fresh isolated scratch
# directories, merge successful results into a new run, record repair
# provenance, and regenerate its scorecard. Gold-bearing scorecards are never
# copied into the repair tree before retries execute.
# The source run is never modified. The original model, provider, effort, Vertex
# project, region, and stage timeouts are restored from its manifest.
uv run clingen-bench retry-failures \
  --run claude_code/<run_id> \
  --max-parallel 4

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
Claude Code, Codex, and Antigravity adapters record the effective model,
provider, effort level, and the source of each resolved value; cloud-backed runs
also record the project and region when available. Codex values are resolved
from explicit `CODEX_*` overrides, the selected profile, or the base user config
in precedence order. Antigravity requires an explicit `AGY_MODEL` and records
either `AGY_EFFORT` or the model's High/Medium/Low suffix. The same provenance
appears at the top of generated scorecards. API keys and credential paths are
never included.

`generate-questions` and `compute-gold` build a bank and its gold answers with an LLM. They are how the bank started, but it is now curated by hand in the workbook, so day to day you want `sync_yaml_from_review.py` instead.

## Writing an agent

Agents are **CLI executables**, called once per (question, stage):

```bash
$ <your_agent> --question-file question.json --output result.json
```

The harness contract remains model- and framework-agnostic, but a coding-agent
adapter must put every model-controlled subprocess through
`agent.isolation.sandboxed_agent_command` and be added to the sandboxed-adapter
registry. Unregistered adapters fail closed. Claude Code, both Codex adapters,
and Antigravity are registered; the template adapter is intentionally not
certified for benchmark runs.

The reference Claude adapter builds a system prompt from
`AGENT_INSTRUCTIONS.md`, then launches `claude` inside the outer namespace with
its normal narrow tool allowlist. Codex also uses its CLI's internal sandbox as
defense in depth. Antigravity relies on the outer boundary because its Linux
`nsjail` cannot execute correctly when nested inside bubblewrap.

Start from `adapters/template/`.

## Repo layout

- `clin_genomic_analysis_benchmark/` — the Python package: CLI `clingen-bench`, evaluation pipeline, scoring
- `clin_genomic_analysis_benchmark/concepts.py` — canonical concept menu and one-time legacy migration rules
- `AGENT_INSTRUCTIONS.md` — the rules the agent under test follows; the authoritative statement of the conventions below
- `questions/<cohort>.yaml` — public, answer-free question banks
- `adapters/` — agent adapters
- `scripts/sync_yaml_from_review.py` — workbook → both YAML banks
- `scripts/migrate_workbook_concepts.py` — one-time prose-concept → canonical-ID workbook migration
- `scoring_configs/default.yaml` — subtask weights, discrepancy thresholds, per-question overrides
- `runs/<agent>/<run_id>/` — evaluation output and `scorecard.{md,json}` (not in git)

Under `$CLINGEN_GOLD_ROOT`, away from the agent:

- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, source of truth
- `questions/<cohort>.yaml` — the gold banks the scorer reads
- `gold_standard/<cohort>/` — a Python script and `result.json` per unambiguous question. The live set is whatever the workbook's `gold_script` column points at; the folders also hold leftovers from removed or reclassified questions
- `bpc_from_synapse/<cohort>/` — cohort data and data dictionaries
- `bpc_benchmark_for_msk_6-20-26/` — a shareable snapshot that intentionally bundles gold

## Conventions the bank assumes

`AGENT_INSTRUCTIONS.md` is authoritative. The ones an operator most needs to know:

- **Key index cancer.** Unless a question says otherwise, each patient is represented by one *key index cancer* — their earliest-diagnosed genomically profiled index cancer. Question stems generally do not contain the word "index"; "patients with `<cohort>` cancer" already means this. Multiple index cancers, and which specimens and regimens belong to which, are resolved by this convention rather than flagged as ambiguous.
- **Genomic-specimen scope.** Genomic facts come from specimens of the cohort's cancer. Patient-level genomic questions use the key index cancer's specimen; sample-level counts use all of that patient's cohort-cancer specimens.
- **Patient-level attributes** — sex, race, ethnicity, vital status — come from `patient_level_dataset.csv`, and anatomic scope does not apply to them.
- **Anatomic and histologic scope** *is* a real ambiguity for cancer-level questions in heterogeneous cohorts (colon vs rectal, bladder vs upper tract) unless the question says which subtypes count.
- **Line of therapy** with no analytic anchor is ambiguous. **"Chemotherapy"** unqualified — cytotoxic only, or any systemic therapy — is ambiguous. So is a **biomarker-frequency denominator** when the question doesn't say whether it's all sequenced patients or only those on gene-covering panels.
- **Delayed cohort entry** (left-truncate at the sequencing date) and **immortal time bias** (use a landmark) have to be addressed on the time-to-event questions where they bite.
- **`dmets_<site>`** is an ever-indicator; at-diagnosis questions use `dx_to_dmets_<site>_days <= 30`. **cBioPortal CNA** values run −2 to 2, where 2 is high-level amplification and −2 a deep deletion. **Clinical-trial regimens** (`drugs_ct_yn == 'Yes'`) are excluded only for drug-specific questions.
- **`analysis_plan_summary`** in the workbook is a self-contained prose method spec for each unambiguous question.

## Pointers

- `README.txt` — plain-language orientation for a non-developer
- `AGENT_INSTRUCTIONS.md` — the rules the agent under test follows
- `bpc_benchmark_review_6-19-26.xlsx` — the curated bank, including the review columns that record its revision history
