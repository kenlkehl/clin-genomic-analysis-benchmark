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
| Gold bank | `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` — answers, concepts, classifications. Read by the scorer, never by the agent |
| Agent guidance | `AGENT_INSTRUCTIONS.md` — served verbatim to the agent by the reference adapter |
| Adapters | `adapters/claude_code/` (Claude Code on Vertex), `adapters/codex_qwen3.6-35B-A3B/` (Codex CLI against a local vLLM server), `adapters/template/` to write your own |

### Keeping the answers away from the agent

The agent under evaluation runs with broad filesystem read access, so **no gold sits in this repo**. The gold question bank, the computed gold answers, and the review workbook all live under **`$CLINGEN_GOLD_ROOT`** (default: sibling directory `../clin-genomic-analysis-benchmark_gold`).

The repo's `questions/<cohort>.yaml` is a projection with the answers stripped out — it is a different Pydantic model with no fields for classification, concepts, or gold answers, so leaking through it is a type error rather than an oversight. Evaluation reads the public bank; only the scorer, a trusted harness process, reads the gold bank.

One residual risk, knowingly accepted: an adversarial agent that walks the filesystem by absolute path could still find the gold root. Close it with an OS sandbox, or point `CLINGEN_GOLD_ROOT` somewhere non-adjacent.

## How scoring works

Every question is graded on **classify**, and then on **either disambiguate or analyze** depending on what the gold says the question is. No single question is graded on all three.

The headline score is the **equal-weighted mean of the three subtasks**, each first expressed as a fraction of its own available points:

```
SCORE = (classify% + disambiguate% + analyze%) / 3
```

It is deliberately not a sum of raw points. The three subtasks sit on very different scales — 211 raw points for classify, 1,668 for disambiguate, 192 for analyze — so a raw sum would make concept-spotting 81% of the benchmark and actual computation 9%. Normalising each subtask first also keeps the balance stable as the bank gains or loses questions. Weights live in `scoring_configs/default.yaml` if you want something other than thirds.

A scorecard looks like this:

```
- SCORE: 68.2% — weighted mean of the three subtasks

| subtask      | earned | possible | score | weight |
| classify     |  172.0 |    211.0 | 81.5% |    33% |
| disambiguate |  750.0 |   1668.0 | 45.0% |    33% |
| analyze      |  150.0 |    192.0 | 78.1% |    33% |
```

**Denominators come from the gold, not from the route the agent took.** If an agent calls an ambiguous question unambiguous, it loses the classify point *and* still owes the disambiguation points it never attempted. Misrouting costs twice, which is the intent.

### 1. Classify

Is this question answerable as written, or does it leave a material analytic choice open? One point for the right label, zero for the wrong one. `AGENT_INSTRUCTIONS.md` sets out the standard in detail; the short version is that a question is unambiguous when a competent BPC analyst would reach for one column, one filter, and one method without asking.

### 2. Disambiguate

Only asked when the agent said the question was ambiguous. The agent lists what a questioner would have to pin down.

Two LLM judges — Claude on Vertex and gpt-5 on Azure — each read the question, our list of gold concepts, and the agent's list. For every gold concept, each judge answers one question: **does the agent's list address this core concept at all?**

| judge answer | points, per judge |
|---|---:|
| yes | 2 |
| unable to determine | 1 |
| no | 0 |

The two judges' points are **summed**, so a gold concept is worth **0–4**. Two `yes` gives 4, a split gives 2, two `no` gives 0.

**The judges are never asked to agree, and nothing waits on a human.** A disagreement lands mid-scale on its own, which is the honest reading of a borderline answer.

The prompts are in `clin_genomic_analysis_benchmark/prompts/`. Three things in them carry most of the weight:

- **"At all"** is the bar. Different wording is fine, and the agent does not have to resolve the issue — naming the choice that has to be made is enough.
- **Touching the same clinical topic without reaching the actual decision is a `no`.** The prompt works through an example: if the concept is whether the sequencing panel even tested a gene, an agent that only discusses which alterations in that gene should count has not addressed it. Those are different problems that happen to share a gene name.
- **One item from the agent can normally only be credited once**, so a single vague statement cannot collect credit across the whole list.

Each judge writes its one-sentence reasoning before its verdict. Every verdict, reason, and raw response is kept — on the scorecard and in `runs/<agent>/<run_id>/per_question/<cohort>/<qid>/judge_*_raw.txt`.

If a judge returns nothing usable for a concept, that judge scores it `unable to determine` and the scorecard reports a `Judge verdicts missing/unparseable` count, so a flaky endpoint is never mistaken for a hard call.

> The gpt-5 judge is called with `max_tokens=16000`. gpt-5 spends most of a small budget on hidden reasoning and then truncates its JSON. If a run reports many missing verdicts, check `judge_azure_raw.txt` for a cut-off response.

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

The source of truth is the **review workbook**, `$CLINGEN_GOLD_ROOT/bpc_benchmark_review_6-19-26.xlsx`: one row per question, holding the classification, question text, disambiguation concepts, analysis plan, expected answer type, gold answer, gold script, and the per-round review columns that record how the bank got here.

The harness never reads the workbook. After any edit, regenerate both YAML banks:

```bash
.venv/bin/python scripts/sync_yaml_from_review.py
```

That validates the workbook against the `Question` schema and writes the gold bank to `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` and the stripped public bank to `questions/<cohort>.yaml`. **Skip it and the agent and scorer are working from a stale bank.**

## Quickstart

```bash
uv sync
cp .env.example .env   # Vertex (ANTHROPIC_VERTEX_PROJECT_ID, credentials) + Azure OpenAI

# Make sure the YAML banks match the workbook
.venv/bin/python scripts/sync_yaml_from_review.py

# Run an agent over one cohort. Each analyze question spawns an agent that runs
# Python against the cohort files, so a full run takes real time and real spend.
uv run clingen-bench eval \
  --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code \
  --cohort bladder_1.2 \
  --max-parallel 4

# Score it. The disambiguation judges need Vertex + Azure.
uv run clingen-bench score --run claude_code/<new_run_id>

# One question, for a smoke test
uv run clingen-bench eval --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code --question bladder_1.2-Q6f9dd68e

# Look at a cohort's data dictionary and file inventory
uv run clingen-bench inspect --cohort bladder_1.2
```

`CLINGEN_CLAUDE_MODEL` (default `claude-opus-4-8`) sets both the reference agent and the Claude judge.

`generate-questions` and `compute-gold` build a bank and its gold answers with an LLM. They are how the bank started, but it is now curated by hand in the workbook, so day to day you want `sync_yaml_from_review.py` instead.

## Writing an agent

Agents are **CLI executables**, called once per (question, stage):

```bash
$ <your_agent> --question-file question.json --output result.json
```

The harness is model- and framework-agnostic — it knows nothing about your agent beyond this contract. The reference adapter (`adapters/claude_code/adapter.py`) builds a system prompt from `AGENT_INSTRUCTIONS.md` and shells out to `claude --print --output-format json --model <model> --add-dir <cohort_dir> --allowedTools Read,Glob,Grep,Bash`.

Start from `adapters/template/`.

## Repo layout

- `clin_genomic_analysis_benchmark/` — the Python package: CLI `clingen-bench`, evaluation pipeline, scoring
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
