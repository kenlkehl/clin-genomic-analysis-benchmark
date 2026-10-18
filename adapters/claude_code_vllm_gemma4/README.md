# Claude Code + Gemma 4 on local vLLM

This adapter runs the benchmark through the Claude Code CLI against a Gemma 4
model served by vLLM. vLLM exposes the Anthropic Messages API (`/v1/messages`)
next to its OpenAI-compatible routes, so Claude Code connects to it directly
through `ANTHROPIC_BASE_URL`; no translation bridge is involved. Prompts, the
tool allow-list, and JSON extraction are the same as the reference
`claude_code` adapter.

Each stage runs in the benchmark's mandatory bubblewrap isolation with a
disposable Claude Code home. No Vertex, Anthropic, or Google credentials are
passed in. Claude Code's Haiku, Sonnet, and Opus aliases and its subagent model
all point at the served model, so background requests never ask vLLM for a
Claude model. Nonessential traffic (telemetry, update checks) is disabled.

The endpoint must not require authentication. Claude Code runs inside the
model-controlled sandbox, so an upstream token would be visible to the model;
the adapter refuses to run when `VLLM_TOKEN` is set.

## Prerequisites

- Claude Code on `PATH` (validated with Claude Code 2.1.302)
- `/usr/bin/bwrap`
- a vLLM server with the Anthropic Messages API and server-side Gemma 4
  tool-call parsing (validated against vLLM 0.31.0)
- the benchmark's normal BPC cohort data and gold-root configuration

Confirm the server answers on the Anthropic route:

```bash
curl http://sn4622130540:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model": "gemma4-26b-a4b", "max_tokens": 32,
       "messages": [{"role": "user", "content": "Say hi"}]}'
```

## Run it

```bash
VLLM_BASE_URL=http://sn4622130540:8000/v1 \
VLLM_MODEL=gemma4-26b-a4b \
uv run clingen-bench eval \
  --agent "bash adapters/claude_code_vllm_gemma4/run.sh" \
  --agent-name claude_code_vllm_gemma4_26b_a4b_nvfp4 \
  --cohort all \
  --max-parallel 4
```

Then score the resulting run:

```bash
uv run clingen-bench score --run <agent-name>/<run_id>
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VLLM_BASE_URL` | required | vLLM API root; a trailing `/v1` is accepted and removed for Claude Code |
| `VLLM_MODEL` | required | served model ID |
| `CLAUDE_BIN` | `claude` | Claude Code executable |

Claude Code assumes a 200,000-token context window for models it does not
recognize, so the server's `max_model_len` should be at least that.

Set these in the shell that launches `clingen-bench` so the harness records the
endpoint and model in `manifest.json` and restores them during failed-run
repair.
