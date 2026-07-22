# clin-genomic-analysis-benchmark

A benchmark for AI agentic workflows / coding agents on **translational clinical cancer-data analysis**, using AACR Project GENIE BPC (Biopharma Collaborative) cohort data.

> Orienting a colleague (non-developer)? See **`README.txt`** for a plain-language tour of the task, `AGENT_INSTRUCTIONS.md`, the review workbook, and the gold scripts. This file is the developer/operator guide.

## Status

End-to-end pipeline complete on all 6 cohorts; reference Claude Code adapter runs on **`claude-opus-4-8`** (Vertex).

| Component | Status |
|---|---|
| Current question bank | **`$CLINGEN_GOLD_ROOT/bpc_benchmark_review_6-19-26.xlsx`** — **211 questions = 96 unambiguous + 115 ambiguous** (after four oncologist-reviewed revision rounds: 6-6, 6-7, 6-8/6-19) |
| Agent-facing bank (public) | `questions/<cohort>.yaml` in the repo — **gold-free** (`id`/`category`/`text` only); this is all the agent under evaluation ever sees |
| Gold bank (scoring) | `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` — full bank with answers/concepts/classification, read only by the scorer. Both banks are regenerated from the workbook by `scripts/sync_yaml_from_review.py` |
| Gold answers | 96 / 96 unambiguous questions have a computed, reproducible gold (`$CLINGEN_GOLD_ROOT/gold_standard/<cohort>/<qid>.py` + `.result.json`); 1 marked `unanswerable` (see below) |
| Agent guidance | `AGENT_INSTRUCTIONS.md` (repo root) — served verbatim to the agent by the reference adapter |
| Reference adapter | `adapters/claude_code/` — Claude Code CLI on Vertex, `--model claude-opus-4-8` |
| Open-model adapter | `adapters/codex_qwen3.6-35B-A3B/` — Codex CLI driving a local vLLM server (`--profile vllm`) |
| Distribution snapshot | `bpc_benchmark_for_msk_6-20-26/` — trimmed workbook + instructions + gold for sharing (intentionally bundles gold) |
| Most recent full scored run | `runs/claude_code/20260512T161428Z-c0f228/` (**predates the current bank** — it was scored on the older v11 bank; a full run on the 6-19 bank has been smoke-tested but not yet completed end-to-end) |

### Benchmark integrity: gold isolation

The agent under evaluation runs with broad filesystem read access, so **no gold may sit in the repo**. All gold-bearing artifacts — the gold question bank, `gold_standard/`, and the review workbook — live under **`$CLINGEN_GOLD_ROOT`** (env-overridable; default sibling `../clin-genomic-analysis-benchmark_gold`), outside the repo. The repo's `questions/<cohort>.yaml` is the gold-free public bank. Eval loads the public bank; only the scorer (a trusted harness process) loads the gold bank. A normal run never exposes gold to the agent. (Residual, deprioritized risk: an adversarial agent that hunts the whole filesystem by absolute path could still read the gold root — close that with an OS sandbox, or point `CLINGEN_GOLD_ROOT` somewhere non-adjacent.)

## What it measures

For each clinical question, an agent is graded on three subtasks:

1. **Classify** the question as `ambiguous` or `unambiguous` — 1 pt correct, 0 pt incorrect.
2. **Disambiguate** (only if the agent says ambiguous) — list the concepts a questioner would need to specify to make the question deterministically answerable. 1 pt per gold concept covered, judged by two independent LLMs (Claude Vertex + Azure OpenAI gpt-5). Disagreements go to `runs/<agent>/<run_id>/review_queue.yaml` with provisional 0.5 pt for human tiebreak.
3. **Analyze** (only if the agent says unambiguous) — compute the answer.
   - 2 pts: ≤5% discrepancy from gold (or correct bucket / exact category match)
   - 1 pt: 5–15% discrepancy
   - 0 pt: >15% discrepancy (or wrong bucket / wrong category / wrong `answer_type`)

Per-answer-type discrepancy rules: relative % for counts/proportions; relative % on the log scale for hazard/odds ratios; bucketed match for p-values; exact match for categoricals; per-category max-deviation for `categorical_distribution` (worst-fitting category sets the band, and the agent's category key set must match gold).

## Cohorts (6)

`bladder_1.2`, `breast_1.2`, `crc_2.0_public`, `nsclc_2.0_public`, `panc_1.2`, `prostate_1.2` — under `bpc_from_synapse/<cohort>/` (read-only, not in git). Questions are scored **per cohort**.

## Source of truth & the bank → YAML sync

The human-curated source of truth is the **review workbook** (`$CLINGEN_GOLD_ROOT/bpc_benchmark_review_6-19-26.xlsx`): one row per question with `classification`, `question_text`, `disambiguation_concepts`, `analysis_plan_summary`, `expected_answer_type`, `gold_answer`, `gold_script`, plus per-round review columns.

The harness does **not** read the workbook. After any edit to the bank, regenerate **both** YAML banks:

```bash
.venv/bin/python scripts/sync_yaml_from_review.py
```

This maps the workbook into the `Question` schema, validates it, and writes:
- the **gold** bank → `$CLINGEN_GOLD_ROOT/questions/<cohort>.yaml` (full; read by the scorer),
- the **public** bank → `questions/<cohort>.yaml` in the repo (gold-free; served to the agent).

**Skipping this step means agents/scorer use a stale bank.**

## Quickstart

```bash
uv sync
cp .env.example .env   # fill in Vertex (ANTHROPIC_VERTEX_PROJECT_ID, creds) + Azure OpenAI (for the judge)

# 0) Make sure the live YAML matches the current bank:
.venv/bin/python scripts/sync_yaml_from_review.py

# Run the reference agent end-to-end on one cohort (each analyze question spawns a Claude agent
# that runs Python on the cohort — a full run takes time + Vertex/Azure cost):
uv run clingen-bench eval \
  --agent "bash adapters/claude_code/run.sh" \
  --agent-name claude_code \
  --cohort bladder_1.2 \
  --max-parallel 4

# Score it (uses the dual-LLM judge for ambiguous questions → needs Vertex + Azure):
uv run clingen-bench score --run claude_code/<new_run_id>

# Single question (handy for smoke tests):
uv run clingen-bench eval --agent "bash adapters/claude_code/run.sh" --agent-name claude_code --question bladder_1.2-Q6f9dd68e

# Inspect a cohort's dictionary + context:
uv run clingen-bench inspect --cohort bladder_1.2
```

`CLINGEN_CLAUDE_MODEL` (default `claude-opus-4-8`) controls both the reference agent and the Claude judge. The LLM question-generation / gold-codegen pipelines (`generate-questions`, `compute-gold`) still exist but are rarely used now — the bank is curated in the workbook and synced to YAML.

## Agent contract

Agents are **CLI executables** invoked once per (question, stage):

```bash
$ <your_agent> --question-file question.json --output result.json
```

The harness is model- and framework-agnostic. The reference adapter (`adapters/claude_code/adapter.py`) builds the system prompt from the repo-root **`AGENT_INSTRUCTIONS.md`** (single source of truth for the conventions the agent must follow) and shells out to `claude --print --output-format json --model <model> --add-dir <cohort_dir> --allowedTools Read,Glob,Grep[,Bash]`. To add your own agent, see `adapters/template/`.

## Repo layout

- `clin_genomic_analysis_benchmark/` — benchmark Python package (CLI `clingen-bench`, pipelines, scoring)
- `AGENT_INSTRUCTIONS.md` — **canonical agent-facing guidance** (served by the adapter); keep this root copy in sync with the distribution copy in `bpc_benchmark_for_msk_6-20-26/`
- `scripts/sync_yaml_from_review.py` — workbook → public + gold `questions/*.yaml` sync
- `adapters/` — agent adapters (reference: `claude_code/`; open-model: `codex_qwen3.6-35B-A3B/`)
- `questions/<cohort>.yaml` — **public, gold-free** per-cohort banks the eval reads (regenerated from the workbook)
- `runs/<agent>/<run_id>/` — evaluation outputs + `scorecard.{md,json}` + `review_queue.yaml`
- `scoring_configs/` — optional band thresholds + per-question overrides (defaults are in code)

**Outside the repo** — under `$CLINGEN_GOLD_ROOT` (default `../clin-genomic-analysis-benchmark_gold`), kept away from the agent:
- `bpc_benchmark_review_6-19-26.xlsx` — current human-curated bank (source of truth)
- `questions/<cohort>.yaml` — the **gold** banks the scorer reads (full: answers, concepts, classification)
- `gold_standard/<cohort>/` — Python script + `result.json` per unambiguous question (active list = the `gold_script` column of the workbook; folders may hold leftover scripts from removed/reclassified questions)
- `bpc_from_synapse/<cohort>/` — cohort data + data dictionaries (read-only, not in git)
- `bpc_benchmark_for_msk_6-20-26/` — shareable snapshot (trimmed workbook, `AGENT_INSTRUCTIONS.md`, gold, `README.txt`)

## Conventions the bank assumes

`AGENT_INSTRUCTIONS.md` is the authoritative, answer-agnostic statement of these. Highlights an operator should know:

- **Key index cancer.** Unless a question explicitly says otherwise, each patient is represented by a single *key index cancer* = their first (earliest-diagnosed) genomically profiled index cancer. Question stems generally do **not** say "index" — "patients with `<cohort>` cancer" already means this. Multiple index cancers and specimen/regimen attribution are resolved by this convention, not flagged as ambiguous.
- **Genomic-specimen scope.** Genomic facts come from specimens of the cohort's cancer; patient-level genomic questions use the key index cancer's specimen, sample-level counts use all of the patient's cohort-cancer specimens.
- **Patient-level attributes** (sex/race/ethnicity/vital status) are computed over `patient_level_dataset.csv`; anatomic/histologic scope does not apply to them.
- **Anatomic/histologic scope** *is* a genuine ambiguity for cancer-level questions in heterogeneous cohorts (e.g. colon vs rectal) unless the question says which subtypes to include.
- **Line of therapy** without an analytic anchor is ambiguous; **"chemotherapy"** unqualified (cytotoxic-only vs any systemic) is ambiguous; **biomarker-frequency denominator** (all sequenced vs gene-covered panels) is ambiguous when unstated.
- **Delayed cohort entry** (left-truncate at sequencing date) and **immortal time bias** (use a landmark analysis) must be addressed for the relevant time-to-event questions.
- **`dmets_<site>`** is an "ever" indicator; at-diagnosis questions use `dx_to_dmets_<site>_days <= 30`. **cBioPortal CNA** values −2/−1/0/1/2 (2 = high-level amp, −2 = deep/homozygous deletion). **Clinical-trial regimens** (`drugs_ct_yn == 'Yes'`) are excluded only for drug-specific questions.
- **`analysis_plan_summary`** is a self-contained prose method spec for each unambiguous question.

## Unanswerable questions

When a question is well-specified but the cohort data are structurally insufficient to identify the estimand (e.g. a Cox interaction whose 2×2 design has a ~empty cell — see `prostate_1.2-Qf17acd7c`, docetaxel × PTEN-homozygous-deletion with one near-empty cell / complete separation), the question stays `classification: unambiguous` and the gold marks `unanswerable: true` (with `n_total`/`n_events`/cell counts and an explanation). The scorer credits agents that flag `unanswerable: true` themselves (or match the placeholder) as ACCURATE; a confidently different value is MAJOR. **Do not** flip such questions to `ambiguous` — the question is fine; the data are insufficient.

## Review queue (manual disambiguation triage)

Items where the two LLM judges disagree on whether the agent covered a gold concept land in `runs/<agent>/<run_id>/review_queue.yaml`, each entry self-contained (question text, agent concepts, the gold concept, both verdicts + justifications, `human_decision: null`). Resolve by setting each `null` to `true`/`false`, then re-score:

```bash
uv run clingen-bench score --run claude_code/<run_id> --resolve-reviews
```

> Note: the Azure (gpt-5) judge is called with a large output budget (`max_tokens=16000` in `clin_genomic_analysis_benchmark/scoring/judge.py`) because gpt-5 spends most of a small budget on hidden reasoning and truncates the verdict JSON. If the judge ever returns all-`needs_review`, check for truncated `judge_azure_raw.txt`.

## Pointers

- `README.txt` — non-developer orientation to the task / instructions / workbook / gold
- `AGENT_INSTRUCTIONS.md` — the rules the agent under test follows
- `bpc_benchmark_review_6-19-26.xlsx` — the current curated bank (+ per-round review columns recording the revision history)
