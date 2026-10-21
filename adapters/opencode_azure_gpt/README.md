# OpenCode + GPT on Azure OpenAI

This adapter runs the benchmark through OpenCode against an OpenAI GPT
deployment on an Azure OpenAI resource, using the Responses API. Select the
deployment with `OPENCODE_AZURE_MODEL` and the reasoning effort with
`OPENCODE_AZURE_REASONING_EFFORT`.

Each stage runs in the benchmark's mandatory bubblewrap isolation with a fresh
OpenCode home and an inline, adapter-owned configuration. The adapter does not
read the user's normal OpenCode configuration, credentials, plugins, skills, or
session history.

OpenCode reaches Azure through a localhost-only bridge started for each
OpenCode invocation. The trusted adapter process obtains a Microsoft Entra
token with `az account get-access-token` and adds it to each forwarded request,
so no Azure credential enters the sandbox. The bridge passes request and
response bodies through unchanged, except that it pins `model` to the configured
deployment. It retries `429` and transient `5xx` responses, honoring
`Retry-After`, and retries a `401` once with a freshly fetched token. It saves
content-free request metadata in each question's
`adapter_audit/opencode_azure_bridge.*.jsonl`: the requested model, reasoning
effort, and storage settings, the effort Azure reports back, status codes, and
token usage.

## Reasoning settings

OpenCode's bundled AI SDK decides from the model name whether a model reasons:
only `o*`, `gpt-5*`, and `codex-*` names count (checked in OpenCode 1.17.4 and
1.19.3). For other names, such as `gpt-6-luna`, it would silently drop the
reasoning effort. OpenCode also applies its GPT reasoning defaults only to
names containing `gpt-5`. The adapter therefore sets these provider options
explicitly for every deployment:

- `forceReasoning: true`
- `reasoningEffort` from `OPENCODE_AZURE_REASONING_EFFORT` (omitted when unset,
  leaving the model default)
- `reasoningSummary: "auto"`
- `store: false` with `include: ["reasoning.encrypted_content"]`, so requests are
  stateless and reasoning is carried between turns, as OpenCode does for GPT-5

Reasoning tokens count against the output cap, and OpenCode caps output at
32,000 tokens by default. The adapter raises the cap to 128,000 tokens through
`OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`. Check the bridge audit's
`reasoning_effort` and `effective_reasoning_effort` fields to confirm the
effort a run actually used.

## Prerequisites

- OpenCode on `PATH` (validated with OpenCode 1.17.4)
- `az` on `PATH`, logged in with access to the deployment (the
  `Cognitive Services OpenAI User` role is enough)
- `/usr/bin/bwrap`
- the benchmark's normal BPC cohort data and gold-root configuration

## Run it

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1 \
OPENCODE_AZURE_MODEL=gpt-6-luna \
OPENCODE_AZURE_REASONING_EFFORT=xhigh \
uv run clingen-bench eval \
  --agent "bash adapters/opencode_azure_gpt/run.sh" \
  --agent-name opencode_azure_gpt_6_luna_xhigh \
  --cohort all \
  --max-parallel 4
```

Set these variables in the shell that launches `clingen-bench`, so the harness
records them in `manifest.json` and restores them during repairs.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | required | resource URL; `/openai/v1` is added when omitted |
| `OPENCODE_AZURE_MODEL` | required | deployment name |
| `OPENCODE_AZURE_REASONING_EFFORT` | unset | `none`, `minimal`, `low`, `medium`, `high`, or `xhigh` |
| `OPENCODE_AZURE_CONTEXT_WINDOW` | `272000` | context length advertised to OpenCode |
| `OPENCODE_MAX_OUTPUT_TOKENS` | `128000` | output limit, reasoning included |
| `OPENCODE_AZURE_REQUEST_TIMEOUT_SECONDS` | `1800` | timeout for one model response |
| `OPENCODE_AZURE_MAX_RETRIES` | `6` | bridge attempts for retryable upstream failures |
| `OPENCODE_AZURE_RETRY_BASE_SECONDS` | `5` | initial exponential-backoff delay |
| `OPENCODE_AZURE_MAX_RETRY_SLEEP_SECONDS` | `60` | cap on one bridge retry delay |
| `OPENCODE_AZURE_MAX_REQUESTS` | `512` | fail-closed request limit per stage |
| `OPENCODE_BIN` | `opencode` | OpenCode executable |
| `OPENCODE_AGENT` | `build` | OpenCode primary agent |
| `OPENCODE_MAX_ATTEMPTS` | `1` | adapter-level retries (the harness already retries stages) |
| `OPENCODE_RETRY_BASE_SECONDS` | `15` | adapter retry delay multiplier |

OpenCode is invoked with `--pure`; sharing and automatic updates are disabled,
and its task, skill, web, and interactive-question tools are denied. Its shell
and file tools remain usable within the outer sandbox, where `/data/cohort` is
read-only and only `/work` is writable.
