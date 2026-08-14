# Codex + Qwen 3.6 35B-A3B on Unsloth Studio

This adapter runs the benchmark with the Codex CLI while sending model requests
to `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` through a local Unsloth Studio server at
`http://127.0.0.1:8888/v1`.

It does not depend on a user Codex profile. Every invocation pins the model,
provider, Responses API, context window, and server URL on the command line and
runs Codex with `--ignore-user-config --ephemeral` inside the benchmark's
mandatory bubblewrap isolation.

## Authentication boundary

Unsloth Studio requires authentication. Create an API key in Studio and export
it before launching the benchmark:

```bash
export UNSLOTH_STUDIO_AUTH_TOKEN='<studio-api-key>'
```

The real key stays in the trusted adapter process. Codex talks to a
localhost-only bridge using a random per-invocation key, so shell commands run
by the model cannot read the Studio credential. `API_TOKEN` remains supported
as a backwards-compatible fallback, but `UNSLOTH_STUDIO_AUTH_TOKEN` is clearer.

Confirm the endpoint and key before a run:

```bash
curl -sS \
  -H "Authorization: Bearer $UNSLOTH_STUDIO_AUTH_TOKEN" \
  http://127.0.0.1:8888/v1/models
```

## Run it

One question:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/run.sh" \
  --agent-name codex_qwen3.6_35b_a3b_unsloth \
  --question bladder_1.2-Q6f9dd68e
```

A full cohort:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl/run.sh" \
  --agent-name codex_qwen3.6_35b_a3b_unsloth \
  --cohort bladder_1.2 \
  --max-parallel 1
```

Start with `--max-parallel 1`; a single loaded local model generally handles
one long agent request more predictably than several concurrent requests.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `UNSLOTH_STUDIO_AUTH_TOKEN` | required | Studio API key; never passed into the Codex sandbox |
| `API_TOKEN` | unset | backwards-compatible token fallback |
| `UNSLOTH_STUDIO_BASE_URL` | `http://127.0.0.1:8888/v1` | Studio OpenAI-compatible API root |
| `UNSLOTH_MODEL` | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` | exact served model ID |
| `CODEX_MODEL` | unset | secondary model override if `UNSLOTH_MODEL` is unset |
| `CODEX_BIN` | `codex` | Codex executable |
| `CODEX_SANDBOX_MODE` | `workspace-write` | analyze-stage Codex sandbox |
| `CODEX_EPHEMERAL` | `1` | disable persistent Codex session state |
| `CODEX_MAX_ATTEMPTS` | `1` | whole-Codex-process attempts inside one harness attempt |
| `CODEX_ANALYZE_CONTINUATIONS` | `2` | same-model continuations from existing scratch work when analyze ends without valid JSON |
| `CODEX_RETRY_BASE_SECONDS` | `15` | delay multiplier between Codex-process attempts |
| `UNSLOTH_REQUEST_TIMEOUT_SECONDS` | `1200` | timeout for one Studio Responses request |
| `UNSLOTH_MAX_RETRIES` | `3` | retryable upstream attempts per Responses request |
| `UNSLOTH_READ_ONLY_MAX_RETRIES` | `1` | upstream attempts for classify/disambiguate before process/finalizer recovery |
| `UNSLOTH_RETRY_BASE_SECONDS` | `2` | exponential upstream retry base |
| `UNSLOTH_MAX_RETRY_SLEEP_SECONDS` | `30` | upstream retry sleep cap |
| `UNSLOTH_MAX_REQUESTS` | `256` | request cap per Codex invocation |
| `UNSLOTH_READ_ONLY_MAX_REQUESTS` | `8` | enforced request cap for classify/disambiguate before finalization fallback |
| `UNSLOTH_READ_ONLY_MAX_OUTPUT_TOKENS` | `8192` | per-generation output cap for classify/disambiguate; analyze is uncapped |

The bridge requests a non-streaming response from Studio, then replays it to
Codex as Responses SSE. This permits safe retries before any partial stream has
been exposed and preserves Studio's native structured tool calls.

Every invocation passes an explicit final-response JSON schema to Codex.
Classify and disambiguate include a bounded-inspection guard. Analyze keeps its
full request and output budgets, but includes a completion guard that directs
the local model to execute fixes immediately and not end on planning prose.
This preserves room for legitimate multi-step computation while making an
unfinished "let me build/run/fix" final message less likely.

If analyze nevertheless ends without contract-valid JSON, the adapter invokes
the same model again in the same scratch directory up to the bounded
`CODEX_ANALYZE_CONTINUATIONS` limit. The continuation is told to use the
existing scripts and outputs, execute the unfinished fix, and return the JSON;
it is not given gold data or an answer from another model.

Before validation, the adapter also normalizes conventional typed-field aliases
such as `hazard_ratio`/`ci_lower`/`ci_upper` to the benchmark's
`value`/`ci_low`/`ci_high` names. This is contract repair only: numeric values,
methods, evidence, and model-selected answer type are otherwise unchanged.

If Codex still ends a classify or disambiguate stage without contract JSON, the
trusted adapter makes a bounded no-tools finalization request to the same Qwen
model using only the public question, stage instructions, and concept menu. It
disables hidden reasoning for this contract-only request so the model cannot
spend its entire output budget before emitting JSON. It does not use gold data,
and its audit records contain only timing/status and output-size metadata—not
prompts, responses, cohort values, or credentials.

Because this local model can take several minutes for one generation and can
make multiple tool-calling turns, the benchmark harness gives this adapter
longer per-stage defaults: 3600 seconds for classify, 3600 for disambiguate,
and 7200 for analyze. Other adapters retain the benchmark-wide defaults.

Adapter logs and bridge metadata are written under the question's
`adapter_audit/` directory. Request bodies, cohort values, and credentials are
not recorded in the bridge audit.
