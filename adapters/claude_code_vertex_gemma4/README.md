# Claude Code + Gemma 4 MaaS on Vertex Agent Platform

This adapter runs the benchmark through the Claude Code CLI against a Gemma 4
MaaS model on Google's Vertex Agent Platform, by default
`google/gemma-4-26b-a4b-it-maas`. It uses the same localhost Anthropic-to-Chat
bridge as `claude_code_vertex_gemini`; see that adapter's README for how the
bridge works and what it keeps out of the sandbox. Prompts, the tool allow-list,
and JSON extraction are the same as the reference `claude_code` adapter.

Gemma returns no thought signatures, and its model card says thinking control is
unsupported, so the adapter sends no reasoning effort and the manifest records
effort as unsupported. Settings use the same `VERTEX_GEMMA_*` names as the
`codex_vertex_gemma4_26b` adapter.

## Prerequisites

- Claude Code on `PATH` (validated with Claude Code 2.1.302)
- `gcloud` on `PATH`, authenticated (`gcloud auth login`)
- access to Gemma 4 MaaS in the selected project
- `/usr/bin/bwrap`
- the benchmark's normal BPC cohort data and gold-root configuration

## Run it

```bash
VERTEX_GEMMA_PROJECT_ID=profile-notes \
uv run clingen-bench eval \
  --agent "bash adapters/claude_code_vertex_gemma4/run.sh" \
  --agent-name claude_code_vertex_gemma4_26b \
  --cohort all \
  --max-parallel 2
```

Set the variables in the shell that launches `clingen-bench` so the harness
records them in `manifest.json` and restores them during failed-run repair.

The MaaS endpoint is shared and is served only from the `global` location. Its
capacity varies: on 2026-10-19 it rejected most large requests with HTTP 429
("The request queue is full") even for a single question, whereas under 1% of
requests were rejected in August 2026. The bridge retries 429s with
exponential backoff, but when rejections are frequent each stage slows sharply.
Run a smoke test (`--question bladder_1.2-Q6f9dd68e --no-retry-failures`) and
check `adapter_audit/vertex_gemma_bridge.*.jsonl` before starting a full run.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VERTEX_GEMMA_PROJECT_ID` | required | Google Cloud project with Gemma MaaS access |
| `VERTEX_GEMMA_MODEL` | `google/gemma-4-26b-a4b-it-maas` | Vertex model ID; a bare ID gets the `google/` prefix |
| `VERTEX_GEMMA_LOCATION` | `global` | Vertex location |
| `VERTEX_GEMMA_MAX_OUTPUT_TOKENS` | `16384` | cap on each response |
| `VERTEX_GEMMA_REQUEST_TIMEOUT_SECONDS` | `600` | timeout for each upstream request |
| `VERTEX_GEMMA_MAX_RETRIES` | `6` | bridge attempts for transient `429` and `5xx` failures |
| `VERTEX_GEMMA_RETRY_BASE_SECONDS` | `5` | initial exponential-backoff delay |
| `VERTEX_GEMMA_MAX_RETRY_SLEEP_SECONDS` | `60` | cap on one retry delay |
| `VERTEX_GEMMA_MAX_REQUESTS` | `256` | fail-closed request limit per stage |
| `CLAUDE_BIN` | `claude` | Claude Code executable |

Content-free request metadata is saved to each question's
`adapter_audit/vertex_gemma_bridge.<stage>.jsonl`.
