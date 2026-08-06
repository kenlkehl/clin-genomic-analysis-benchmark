# Antigravity Gemini adapter

This model-neutral adapter evaluates Gemini coding agents through Google
Antigravity CLI print mode. Every invocation requires an explicit model, runs
inside the benchmark's fail-closed bubblewrap boundary, and receives a fresh
Antigravity home with no prior conversations, memories, implicit context,
knowledge, logs, or trusted host workspaces.

## Prerequisites

1. Install and authenticate `agy` 1.1.8 or newer (required for structured
   output); verify it with `agy --version`.
2. Run `agy models` and copy the desired model name exactly into `AGY_MODEL`.
3. For enterprise authentication, configure the approved GCP project during
   Antigravity onboarding or set `AGY_GCP_PROJECT` and `AGY_GCP_LOCATION`.

As of `agy` 1.1.10 on this benchmark host, the relevant names are:

```text
gemini-3.6-flash-high
gemini-3.6-flash-medium
gemini-3.6-flash-low
gemini-3.5-flash
gemini-3.5-flash-medium
gemini-3.5-flash-low
```

That CLI version does not list a model literally named “Gemini 3.5 Flash
Lite.” Do not assume that `Low` and `Lite` are aliases; use a Lite name only
after it appears in `agy models` for the installed CLI/account.

On this account, `gemini-3.5-flash` currently accepts only `low` and `medium`
through `--effort`; `AGY_EFFORT=high` is rejected by the service before a model
call. Treat `agy`'s runtime availability error as authoritative because effort
availability can vary independently of the display names in documentation.

## Configuration

| variable | default | purpose |
|---|---|---|
| `AGY_MODEL` | required | exact model/effort variant from `agy models` |
| `AGY_EFFORT` | model default | optional explicit `low`, `medium`, or `high` reasoning effort |
| `AGY_GCP_PROJECT` | sanitized local setting | override the GCP project copied into the ephemeral home |
| `AGY_GCP_LOCATION` | sanitized local setting | override the GCP location copied into the ephemeral home |
| `AGY_AGENT` | unset | optional named Antigravity agent profile |
| `AGY_MODE` | `accept-edits` | `accept-edits` or `plan` |
| `AGY_BIN` | `agy` | Antigravity CLI executable used by trusted adapter code |
| `AGY_CLI_DISABLE_AUTO_UPDATE` | forced to `true` | prevents the CLI binary changing during a benchmark run |
| `AGY_USE_SANDBOX` | `0` | experimental nested Antigravity sandbox; leave disabled for benchmark runs |
| `AGY_SKIP_PERMISSIONS` | `0` | emergency diagnostic override; avoid in benchmark runs because it bypasses Antigravity permission checks |
| `AGY_PRINT_TIMEOUT_CLASSIFY` | `9m30s` | Antigravity print timeout for classify |
| `AGY_PRINT_TIMEOUT_DISAMBIGUATE` | `4m30s` | Antigravity print timeout for disambiguate |
| `AGY_PRINT_TIMEOUT_ANALYZE` | `29m30s` | Antigravity print timeout for analyze |
| `AGY_PRINT_TIMEOUT` | unset | fallback Antigravity print timeout for all stages |

The adapter reads safe runtime fields from
`~/.gemini/antigravity-cli/settings.json`, but it does not mount that directory.
It constructs a disposable config containing only the pinned model, GCP
project/location, non-secret installation identifiers, telemetry disabled, and
`/work` as the sole trusted workspace. Trusted setup reads only Antigravity's
exact `service=gemini, username=antigravity` OAuth profile from the host Secret
Service and writes it to Antigravity's disposable token-file fallback. The host
D-Bus and all other keyring entries remain inaccessible. Google
application-default credentials are also copied for GCP-backed authentication.
`CLINGEN_AGY_CONFIG_DIR` can select another source config for testing.

## Smoke test

```bash
AGY_MODEL=gemini-3.6-flash-high \
uv run clingen-bench eval \
  --agent "bash adapters/antigravity_gemini/run.sh" \
  --agent-name antigravity_gemini_36_flash_high \
  --question bladder_1.2-Q6f9dd68e \
  --max-parallel 1
```

## Full benchmark

```bash
AGY_MODEL=gemini-3.6-flash-high \
uv run clingen-bench eval \
  --agent "bash adapters/antigravity_gemini/run.sh" \
  --agent-name antigravity_gemini_36_flash_high \
  --cohort all \
  --max-parallel 4
```

Use a different `--agent-name` for every model variant so prior runs are
preserved. The manifest and scorecard record the exact Antigravity model,
explicit effort or the High/Medium/Low model variant when present, project,
location, mode, and agent profile. Credentials and source config paths are
excluded.

## Isolation and audit artifacts

The model process sees only `/data/cohort` (read-only), the current dictionary,
`/work` (writable per-question scratch), an ephemeral home, and the software
runtime. The repository, gold root, previous runs, host home, and sibling files
are absent. Antigravity stdout, stderr, CLI logs, and generated session state are
preserved under each question's `adapter_audit/` directory after the process
exits; they are outside the mount used by later stages.

The disposable settings allow normal analysis commands while web/MCP tools and
direct file-tool reads of the disposable credential directories are denied.
The mandatory outer bubblewrap namespace is the authoritative containment
boundary. Antigravity CLI 1.1.10's Linux `nsjail` cannot start its shell when
nested inside that namespace, so `AGY_USE_SANDBOX` defaults off. Do not enable
`AGY_SKIP_PERMISSIONS` for scored runs.
