# Adapter: `codex_qwen3.6-35B-A3B`

Agent/harness combo = **OpenAI Codex CLI** as the agent harness, driving a local
**Unsloth Studio** server that serves an open-source model
(`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`) on `http://127.0.0.1:8888/v1`. It mirrors
the interactive `codex --profile unsloth_api` workflow.

## How it works

For each `(question, stage)` the harness calls:

```
run.sh --question-file <question.json> --output <result.json>
```

`adapter.py` then:

1. Builds the prompt = repo-root **`AGENT_INSTRUCTIONS.md`** (the single source
   of truth for conventions + answer schemas, same doc the Claude adapter serves
   as a system prompt) followed by the stage-specific question payload.
2. Runs `codex exec --profile unsloth_api` non-interactively, feeding the
   prompt on stdin, with the per-question **scratch dir as the working root**
   (`-C`).
3. Reads Codex's final message from `--output-last-message` and extracts the
   contract JSON (tolerant parser: fenced → brace-balanced → truncation repair).
4. Writes `result.json`.

### Sandbox per stage

| Stage | `--sandbox` | Why |
|---|---|---|
| classify, disambiguate | `read-only` | only inspects the data dictionary / file headers |
| analyze | `workspace-write` | runs Python, writes intermediates to scratch; reads the cohort read-only |

Under `workspace-write`, the agent can **read** anywhere but only **write**
inside the scratch (working) dir — so the read-only cohort data is protected.
If your platform's sandbox blocks reads of the cohort dir, set
`CODEX_SANDBOX_MODE=danger-full-access`.

## Prerequisites

1. **Codex CLI** on PATH (developed against `codex-cli 0.142.5`).
2. A **`unsloth_api` Codex profile**. This adapter also passes provider
   overrides, but it is designed to match:
   - `[model_providers.unsloth_api]` in `~/.codex/config.toml`
     (`base_url = http://127.0.0.1:8888/v1`, `wire_api = "responses"`,
     `env_key = "UNSLOTH_STUDIO_AUTH_TOKEN"`, `requires_openai_auth = false`)
   - `~/.codex/unsloth_api.config.toml` setting
     `model_provider = "unsloth_api"` and
     `model = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"`
3. The **Unsloth Studio server running** and serving that model at the profile's
   `base_url`. Sanity check: `curl http://127.0.0.1:8888/v1/models`.
4. `UNSLOTH_STUDIO_AUTH_TOKEN` exported (or in repo-root `.env`) if your server
   requires auth. If only `API_TOKEN` is set, the adapter copies it to
   `UNSLOTH_STUDIO_AUTH_TOKEN` before invoking Codex.

## Configuration (env vars, all optional)

| Var | Default | Purpose |
|---|---|---|
| `UNSLOTH_STUDIO_AUTH_TOKEN` | `API_TOKEN` or `EMPTY` | auth token for the Unsloth Studio endpoint |
| `API_TOKEN` | `EMPTY` | fallback auth token copied to `UNSLOTH_STUDIO_AUTH_TOKEN` |
| `CODEX_PROFILE` | `unsloth_api` | Codex profile to layer |
| `CODEX_MODEL_PROVIDER` | `unsloth_api` | Codex model provider key |
| `UNSLOTH_STUDIO_BASE_URL` | `http://127.0.0.1:8888/v1` | Unsloth Studio OpenAI-compatible base URL |
| `CODEX_PROVIDER_ENV_KEY` | `UNSLOTH_STUDIO_AUTH_TOKEN` | env var Codex uses for provider auth |
| `CODEX_MODEL` | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` | override the served model name |
| `CODEX_SANDBOX_MODE` | `workspace-write` | sandbox for the analyze stage |
| `CODEX_BIN` | `codex` | path to the codex binary |

`run.sh` sources repo-root `.env` before invoking (the harness does not).

## Run it

```bash
# smoke test one question
uv run clingen-bench eval \
  --agent "bash adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/run.sh" \
  --agent-name codex_qwen3.6-35B-A3B \
  --question bladder_1.2-Q6f9dd68e

# a full cohort
.venv/bin/clingen-bench eval \
  --agent "bash adapters/codex_qwen3.6_35B_A3B_GGUF/run.sh" \
  --agent-name codex_qwen3.6-35B-A3B \
  --cohort all \
  --max-parallel 4

# then score
uv run clingen-bench score --run codex_qwen3.6-35B-A3B/<run_id>
```

Per-question artifacts (including `<stage>.agent.log` with Codex's stdout/stderr
and `.codex_last_message.<stage>.txt` in the scratch dir) land under
`runs/codex_qwen3.6-35B-A3B/<run_id>/per_question/<cohort>/<qid>/`.

> **Privacy note:** unlike the cloud adapters, this keeps cohort data entirely
> local (Codex ↔ your Unsloth Studio server on localhost) — no external endpoint
> sees the data. The analyze sandbox also disables the agent shell's network
> access.
