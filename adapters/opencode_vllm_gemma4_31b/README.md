# OpenCode + Gemma 4 31B on Camus vLLM

This adapter runs the benchmark through OpenCode using the OpenAI-compatible
vLLM deployment on Camus:

- API root: `http://camus.dfci.harvard.edu:8002/v1`
- served model ID: `gemma4-31b`
- backing model: `RedHatAI/Gemma-4-31B-IT-FP8-Dynamic`
- context window: 262,144 tokens

Each stage runs in the benchmark's mandatory bubblewrap isolation with a fresh
OpenCode home and an inline, adapter-owned configuration. The adapter uses
OpenCode's `@ai-sdk/openai` Responses client and does not read the user's normal
OpenCode configuration, credentials, plugins, skills, or session history.

The current vLLM deployment emits Gemma 4's native `<|tool_call>...` markup as
ordinary text. The adapter starts an authenticated localhost-only compatibility
bridge for each OpenCode invocation. It repairs that markup into standard
Responses tool-call events, allowing OpenCode to execute shell and file tools.
The upstream vLLM token, when configured, remains in the trusted adapter process
and is not exposed to OpenCode.

## Prerequisites

- OpenCode on `PATH` (validated with OpenCode 1.17.4)
- `/usr/bin/bwrap`
- network access to `camus.dfci.harvard.edu:8002`
- the benchmark's normal BPC cohort data and gold-root configuration

Confirm the server is available:

```bash
curl http://camus.dfci.harvard.edu:8002/v1/models
```

## Run it

Smoke-test one question:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/opencode_vllm_gemma4_31b/run.sh" \
  --agent-name opencode_vllm_gemma4_31b \
  --question bladder_1.2-Q6f9dd68e \
  --no-retry-failures
```

Run the full benchmark:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/opencode_vllm_gemma4_31b/run.sh" \
  --agent-name opencode_vllm_gemma4_31b \
  --cohort all \
  --max-parallel 4
```

Then score the resulting run:

```bash
uv run clingen-bench score --run opencode_vllm_gemma4_31b/<run_id>
```

## Configuration

All settings are optional:

| Variable | Default | Purpose |
|---|---|---|
| `VLLM_BASE_URL` | `http://camus.dfci.harvard.edu:8002/v1` | OpenAI-compatible API root; `/v1` is added when omitted |
| `VLLM_MODEL` | `gemma4-31b` | served model ID |
| `VLLM_TOKEN` | unset | optional upstream bearer token; never written to provenance or passed to OpenCode |
| `OPENCODE_BIN` | `opencode` | OpenCode executable |
| `OPENCODE_AGENT` | `build` | OpenCode primary agent |
| `OPENCODE_MAX_OUTPUT_TOKENS` | `32768` | model output limit advertised to OpenCode |
| `OPENCODE_MAX_ATTEMPTS` | `1` | adapter-level retries (the harness already retries stages) |
| `OPENCODE_RETRY_BASE_SECONDS` | `15` | adapter retry delay multiplier |
| `VLLM_REQUEST_TIMEOUT_SECONDS` | `600` | timeout for each upstream inference request |
| `VLLM_MAX_RETRIES` | `3` | bridge retries for transient upstream failures |
| `VLLM_RETRY_BASE_SECONDS` | `2` | initial exponential-backoff delay |
| `VLLM_MAX_RETRY_SLEEP_SECONDS` | `30` | cap on one bridge retry delay |
| `VLLM_MAX_REQUESTS` | `256` | fail-closed tool-loop request limit per stage |

OpenCode is invoked with `--pure`; sharing and automatic updates are disabled,
and its task, skill, web, and interactive-question tools are denied. Its shell
and file tools remain usable within the outer sandbox, where `/data/cohort` is
read-only and only `/work` is writable.
