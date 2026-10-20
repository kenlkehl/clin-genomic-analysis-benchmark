# Claude Code + Gemini on Vertex Agent Platform

This adapter runs the benchmark through the Claude Code CLI against a Gemini
model on Google's Vertex Agent Platform. Claude Code speaks the Anthropic
Messages API, while Vertex serves Gemini through its OpenAI-compatible Chat
Completions endpoint. Each Claude Code invocation therefore starts a trusted,
localhost-only bridge (`anthropic_bridge.py`) that translates between the two.
Upstream calls, token refresh, and retries reuse the
`codex_vertex_gemma4_26b` bridge. Prompts, the tool allow-list, and JSON
extraction are the same as the reference `claude_code` adapter.

The bridge obtains short-lived tokens with `gcloud auth print-access-token`.
Google credentials and the host gcloud home are never mounted into the
model-controlled sandbox; Claude Code receives only an ephemeral token for the
local bridge. Claude Code's Haiku, Sonnet, and Opus aliases and its subagent
model all point at the Gemini model, and nonessential traffic is disabled.

Gemini 3 attaches a thought signature to its tool calls and rejects a replayed
call without it (HTTP 400). Anthropic `tool_use` blocks have no field for one,
so the bridge records each signature under the `tool_use` id it gives Claude
Code and reattaches it when the call comes back. A call the bridge did not issue
gets Google's documented placeholder signature; the per-stage audit records how
often that happened (`signature_placeholders`).

## Prerequisites

- Claude Code on `PATH` (validated with Claude Code 2.1.302)
- `gcloud` on `PATH`, authenticated (`gcloud auth login`)
- access to the Gemini model in the selected project
- `/usr/bin/bwrap`
- the benchmark's normal BPC cohort data and gold-root configuration

## Run it

Smoke-test one question:

```bash
VERTEX_GEMINI_PROJECT_ID=profile-notes \
VERTEX_GEMINI_MODEL=google/gemini-3.8-flash \
VERTEX_GEMINI_REASONING_EFFORT=high \
uv run clingen-bench eval \
  --agent "bash adapters/claude_code_vertex_gemini/run.sh" \
  --agent-name claude_code_vertex_gemini_3.8_flash_high \
  --question bladder_1.2-Q6f9dd68e \
  --no-retry-failures
```

For the full benchmark, replace `--question ... --no-retry-failures` with
`--cohort all --max-parallel 4`. Then score the resulting run:

```bash
uv run clingen-bench score --run <agent-name>/<run_id>
```

Set the variables in the shell that launches `clingen-bench` so the harness
records them in `manifest.json` and restores them during failed-run repair.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VERTEX_GEMINI_PROJECT_ID` | required | Google Cloud project with Gemini access |
| `VERTEX_GEMINI_MODEL` | required | Vertex model ID; a bare ID gets the `google/` prefix |
| `VERTEX_GEMINI_LOCATION` | `global` | Vertex location |
| `VERTEX_GEMINI_REASONING_EFFORT` | model default | `minimal`, `low`, `medium`, or `high` |
| `VERTEX_GEMINI_MAX_OUTPUT_TOKENS` | `65536` | cap on each response, reasoning included |
| `VERTEX_GEMINI_REQUEST_TIMEOUT_SECONDS` | `600` | timeout for each upstream request |
| `VERTEX_GEMINI_MAX_RETRIES` | `6` | bridge attempts for transient `429` and `5xx` failures |
| `VERTEX_GEMINI_RETRY_BASE_SECONDS` | `5` | initial exponential-backoff delay |
| `VERTEX_GEMINI_MAX_RETRY_SLEEP_SECONDS` | `60` | cap on one retry delay |
| `VERTEX_GEMINI_MAX_REQUESTS` | `256` | fail-closed request limit per stage |
| `CLAUDE_BIN` | `claude` | Claude Code executable |

The bridge buffers each upstream answer and then replays it to Claude Code as
an Anthropic event stream. It drops Claude Code's `thinking` settings, relying on
`VERTEX_GEMINI_REASONING_EFFORT` instead. Images in tool results (for example a
plot read back with `Read`) are forwarded to Gemini as image parts; documents are
replaced with a placeholder. Claude Code assumes a 200,000-token context window
for models it does not recognize, which is within Gemini's.

Content-free request metadata is saved to each question's
`adapter_audit/vertex_gemini_bridge.<stage>.jsonl`.
