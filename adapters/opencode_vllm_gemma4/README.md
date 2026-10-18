# OpenCode + Gemma 4 on local vLLM

This adapter runs the benchmark through OpenCode using an OpenAI-compatible
vLLM server. It works with any Gemma 4 variant; select the server and model
with `VLLM_BASE_URL`, `VLLM_MODEL`, and `VLLM_CONTEXT_WINDOW`. Its defaults
match the Camus deployment of Gemma 4 31B:

- API root: `http://camus.dfci.harvard.edu:8002/v1`
- served model ID: `gemma4-31b`
- backing model: `RedHatAI/Gemma-4-31B-IT-FP8-Dynamic`
- context window: 262,144 tokens

See `adapters/codex_vllm_gemma4/README.md` for the known deployments.

Each stage runs in the benchmark's mandatory bubblewrap isolation with a fresh
OpenCode home and an inline, adapter-owned configuration. The adapter uses
OpenCode's `@ai-sdk/openai` Responses client and does not read the user's normal
OpenCode configuration, credentials, plugins, skills, or session history.

The Camus vLLM deployment emits Gemma 4's native `<|tool_call>...` markup as
ordinary text. The adapter starts an authenticated localhost-only compatibility
bridge for each OpenCode invocation. It repairs that markup into standard
Responses tool-call events, allowing OpenCode to execute shell and file tools.
The upstream vLLM token, when configured, remains in the trusted adapter process
and is not exposed to OpenCode.

## Prerequisites

- OpenCode on `PATH` (validated with OpenCode 1.17.4)
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
  --agent "bash adapters/opencode_vllm_gemma4/run.sh" \
  --agent-name opencode_vllm_gemma4_31b \
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
  --agent "bash adapters/opencode_vllm_gemma4/run.sh" \
  --agent-name opencode_vllm_gemma4_26b_a4b_nvfp4 \
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
| `VLLM_CONTEXT_WINDOW` | `262144` | context length advertised to OpenCode; match the server's `max_model_len` |
| `VLLM_TOKEN` | unset | optional upstream bearer token; never written to provenance or passed to OpenCode |
| `OPENCODE_BIN` | `opencode` | OpenCode executable |
| `OPENCODE_AGENT` | `build` | OpenCode primary agent |
| `OPENCODE_MAX_OUTPUT_TOKENS` | `32768` | model output limit advertised to OpenCode |
| `OPENCODE_MAX_ATTEMPTS` | `1` | adapter-level retries (the harness already retries stages) |
| `OPENCODE_RETRY_BASE_SECONDS` | `15` | adapter retry delay multiplier |
| `OPENCODE_REASONING_FALLBACK` | `1` | recover tool calls or text from the reasoning when a turn has nothing else (see below); `0` disables |
| `VLLM_REQUEST_TIMEOUT_SECONDS` | `600` | timeout for each upstream inference request |
| `VLLM_MAX_RETRIES` | `3` | bridge retries for transient upstream failures |
| `VLLM_RETRY_BASE_SECONDS` | `2` | initial exponential-backoff delay |
| `VLLM_MAX_RETRY_SLEEP_SECONDS` | `30` | cap on one bridge retry delay |
| `VLLM_MAX_REQUESTS` | `256` | fail-closed tool-loop request limit per stage |

Gemma 4 sometimes ends a turn with all of its output in the reasoning channel
and no message or tool call. The bridge normally drops reasoning, so OpenCode
would receive an empty turn and finish with no answer. In the cases inspected,
the reasoning held a tool call, or the remnant of one, that had lost its
`<|tool_call>` start token. With `OPENCODE_REASONING_FALLBACK` on, the bridge
handles such turns only: tool calls found in the reasoning become real tool
calls, and otherwise the reasoning text is sent as the assistant message. It
records `"reasoning_promoted": true` in that stage's
`adapter_audit/opencode_vllm_bridge.*.jsonl`. Runs made before this fallback
existed did not have it. The Codex adapter shares the bridge but leaves the
fallback off.

OpenCode is invoked with `--pure`; sharing and automatic updates are disabled,
and its task, skill, web, and interactive-question tools are denied. Its shell
and file tools remain usable within the outer sandbox, where `/data/cohort` is
read-only and only `/work` is writable.
