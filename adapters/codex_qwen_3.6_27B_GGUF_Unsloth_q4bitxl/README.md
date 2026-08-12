# Codex + Qwen 3.6 27B on Unsloth Studio

This adapter is the Qwen 3.6 27B counterpart of the existing 35B-A3B adapter.
It runs the benchmark with the Codex CLI while sending model requests to the
`unsloth/Qwen3.6-27B-MTP-GGUF` model served by local Unsloth Studio at
`http://127.0.0.1:8888/v1`. The verified local quantization is `UD-Q4_K_XL`.

It shares the 35B adapter's trusted Responses bridge, retry behavior,
contract-finalization fallback, and mandatory bubblewrap isolation. The Studio
credential stays in the trusted adapter process and is never exposed to Codex
or to model-launched shell commands.

## Authentication

Create an API key in Studio and export it before launching the benchmark:

```bash
export UNSLOTH_STUDIO_AUTH_TOKEN='<studio-api-key>'
```

Confirm that the expected model is loaded:

```bash
curl -sS \
  -H "Authorization: Bearer $UNSLOTH_STUDIO_AUTH_TOKEN" \
  http://127.0.0.1:8888/v1/models
```

## Run it

One question:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_qwen_3.6_27B_GGUF_Unsloth_q4bitxl/run.sh" \
  --agent-name codex_qwen3.6_27b_unsloth \
  --question bladder_1.2-Q6f9dd68e
```

A full cohort:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_qwen_3.6_27B_GGUF_Unsloth_q4bitxl/run.sh" \
  --agent-name codex_qwen3.6_27b_unsloth \
  --cohort bladder_1.2 \
  --max-parallel 1
```

Start with `--max-parallel 1`; one loaded local model generally handles a
single long agent request more predictably than concurrent requests.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `UNSLOTH_STUDIO_AUTH_TOKEN` | required | Studio API key; never passed into the Codex sandbox |
| `API_TOKEN` | unset | backwards-compatible token fallback |
| `UNSLOTH_STUDIO_BASE_URL` | `http://127.0.0.1:8888/v1` | Studio OpenAI-compatible API root |
| `UNSLOTH_MODEL` | `unsloth/Qwen3.6-27B-MTP-GGUF` | exact served model ID |
| `CODEX_MODEL` | unset | secondary model override if `UNSLOTH_MODEL` is unset |
| `CODEX_BIN` | `codex` | Codex executable |
| `CODEX_SANDBOX_MODE` | `workspace-write` | analyze-stage Codex sandbox |
| `CODEX_EPHEMERAL` | `1` | disable persistent Codex session state |
| `CODEX_MAX_ATTEMPTS` | `1` | whole-Codex-process attempts inside one harness attempt |
| `CODEX_RETRY_BASE_SECONDS` | `15` | delay multiplier between Codex-process attempts |
| `UNSLOTH_REQUEST_TIMEOUT_SECONDS` | `1200` | timeout for one Studio Responses request |
| `UNSLOTH_MAX_RETRIES` | `3` | retryable upstream attempts per Responses request |
| `UNSLOTH_READ_ONLY_MAX_RETRIES` | `1` | upstream attempts for classify/disambiguate |
| `UNSLOTH_RETRY_BASE_SECONDS` | `2` | exponential upstream retry base |
| `UNSLOTH_MAX_RETRY_SLEEP_SECONDS` | `30` | upstream retry sleep cap |
| `UNSLOTH_MAX_REQUESTS` | `256` | request cap per Codex invocation |
| `UNSLOTH_READ_ONLY_MAX_REQUESTS` | `8` | request cap for classify/disambiguate |
| `UNSLOTH_READ_ONLY_MAX_OUTPUT_TOKENS` | `8192` | output cap for classify/disambiguate |

The benchmark uses longer per-stage defaults for this local adapter: 3600
seconds for classify, 3600 for disambiguate, and 7200 for analyze. Adapter logs
and non-sensitive bridge metadata are written under each question's
`adapter_audit/` directory.
