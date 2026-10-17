# Codex + Gemma 4 on local vLLM

This adapter runs the benchmark through the OpenAI Codex CLI while using the
OpenAI-compatible Responses API exposed by a vLLM server. It works with any
Gemma 4 variant; select the server and model with `VLLM_BASE_URL`,
`VLLM_MODEL`, and `VLLM_CONTEXT_WINDOW`. Its defaults match the Camus
deployment of Gemma 4 31B:

- API root: `http://camus.dfci.harvard.edu:8002/v1`
- served model ID: `gemma4-31b`
- backing model: `RedHatAI/Gemma-4-31B-IT-FP8-Dynamic`
- context window: 262,144 tokens

Known deployments:

| Model | `VLLM_BASE_URL` | `VLLM_MODEL` | `VLLM_CONTEXT_WINDOW` |
|---|---|---|---|
| `RedHatAI/Gemma-4-31B-IT-FP8-Dynamic` | `http://camus.dfci.harvard.edu:8002/v1` | `gemma4-31b` | `262144` (default) |
| `nvidia/Gemma-4-26B-A4B-NVFP4` | `http://sn4622130540:8000/v1` | `gemma4-26b-a4b` | `250000` |

Check `max_model_len` in the server's `/v1/models` response and set
`VLLM_CONTEXT_WINDOW` to match.

The adapter does not depend on or modify `~/.codex/config.toml`. Each stage runs
in the benchmark's mandatory bubblewrap isolation with a disposable Codex home.
The cohort is read-only and only the per-question scratch directory is writable
during an analysis stage.

The Camus server returns Gemma 4's native `<|tool_call>...` markup as ordinary
response text rather than parsed Responses API tool-call items. For each Codex
invocation, the adapter therefore starts an authenticated, localhost-only
compatibility bridge. It forwards requests to vLLM, converts raw Gemma tool
markup into standard Responses events, and lets Codex execute the requested
shell or patch tool normally. An already parsed vLLM response passes through
unchanged. Bridge audit records contain request metadata and counts, not prompts
or tool content.

vLLM's documented alternative is to relaunch the server with
`--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4`
and the Gemma 4 tool chat template. See the official
[vLLM Gemma 4 usage guide](https://docs.vllm.ai/projects/recipes/en/stable/Google/Gemma4.html).

## Prerequisites

- Codex CLI on `PATH` (developed against `codex-cli 0.147.0`)
- `/usr/bin/bwrap`
- network access to the vLLM server
- the benchmark's normal BPC cohort data and gold-root configuration

Confirm the server is available and note its model ID and `max_model_len`:

```bash
curl http://camus.dfci.harvard.edu:8002/v1/models
```

## Run it

Smoke-test one question (against the default 31B deployment):

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_vllm_gemma4/run.sh" \
  --agent-name codex_vllm_gemma4_31b \
  --question bladder_1.2-Q6f9dd68e \
  --no-retry-failures
```

Run the full benchmark against another Gemma 4 deployment. Use an
`--agent-name` that identifies the model so its runs land in their own
directory:

```bash
VLLM_BASE_URL=http://sn4622130540:8000/v1 \
VLLM_MODEL=gemma4-26b-a4b \
VLLM_CONTEXT_WINDOW=250000 \
uv run clingen-bench eval \
  --agent "bash adapters/codex_vllm_gemma4/run.sh" \
  --agent-name codex_vllm_gemma4_26b_a4b_nvfp4 \
  --cohort all \
  --max-parallel 4
```

Then score the resulting run:

```bash
uv run clingen-bench score --run <agent-name>/<run_id>
```

## Configuration

All settings are optional:

| Variable | Default | Purpose |
|---|---|---|
| `VLLM_BASE_URL` | `http://camus.dfci.harvard.edu:8002/v1` | OpenAI-compatible API root; `/v1` is added when omitted |
| `VLLM_MODEL` | `gemma4-31b` | served model ID |
| `VLLM_CONTEXT_WINDOW` | `262144` | context length Codex assumes; match the server's `max_model_len` |
| `CODEX_MODEL` | unset | fallback model override when `VLLM_MODEL` is unset |
| `VLLM_TOKEN` | unset | optional bearer token; never written to run provenance |
| `CODEX_BIN` | `codex` | Codex executable |
| `CODEX_SANDBOX_MODE` | stage-dependent | override Codex's inner sandbox mode |
| `CODEX_MAX_ATTEMPTS` | `1` | optional adapter-level retries (the harness already retries stages) |
| `VLLM_REQUEST_TIMEOUT_SECONDS` | `600` | timeout for each upstream inference request |
| `VLLM_MAX_RETRIES` | `3` | bridge retries for transient upstream failures |
| `VLLM_RETRY_BASE_SECONDS` | `2` | initial exponential-backoff delay |
| `VLLM_MAX_RETRY_SLEEP_SECONDS` | `30` | cap on one bridge retry delay |
| `VLLM_MAX_REQUESTS` | `256` | fail-closed tool-loop request limit per stage |

Set overrides in the shell that launches `clingen-bench`, rather than only in
an adapter-local shell. This lets the harness record the non-secret endpoint and
model in `manifest.json` and restore them during failed-run repair. `VLLM_TOKEN`
is deliberately excluded from manifests and logs. Codex receives only an
ephemeral key for the localhost bridge; the upstream token remains in the
trusted adapter process outside the model-controlled sandbox.
