# Antigravity Gemini 3.5 Flash adapter

This adapter lets `clingen-bench` evaluate Google Antigravity CLI in print mode.
The local Antigravity settings should select Gemini 3.5 Flash; on this machine
that is recorded in:

```text
~/.gemini/antigravity-cli/settings.json
```

## Contract

The benchmark harness calls:

```bash
bash adapters/antigravity_gemini_3.5_flash/run.sh \
  --question-file <abs question.json> \
  --output <abs result.json>
```

The adapter builds a prompt from `AGENT_INSTRUCTIONS.md` plus the stage payload,
runs:

```bash
agy --print ...
```

and writes the final JSON object to the requested output path.

## Defaults

| variable | default | purpose |
|---|---|---|
| `AGY_BIN` | `agy` | Antigravity CLI executable |
| `AGY_USE_SANDBOX` | `1` | pass `--sandbox` to Antigravity |
| `AGY_SKIP_PERMISSIONS` | `1` | pass `--dangerously-skip-permissions` so non-interactive runs do not hang |
| `AGY_PRINT_TIMEOUT_CLASSIFY` | `9m30s` | print-mode timeout for classify |
| `AGY_PRINT_TIMEOUT_DISAMBIGUATE` | `4m30s` | print-mode timeout for disambiguate |
| `AGY_PRINT_TIMEOUT_ANALYZE` | `29m30s` | print-mode timeout for analyze |
| `AGY_PRINT_TIMEOUT` | unset | fallback timeout for all stages |
| `AGY_LOG_FILE` | `<scratch>/agy.<stage>.log` | Antigravity CLI log path |

The `run.sh` wrapper prepends these common install locations to `PATH`:

```text
~/.local/bin
~/.gemini/antigravity-cli/bin
```

## Smoke test

Run one question:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/antigravity_gemini_3.5_flash/run.sh" \
  --agent-name antigravity_gemini_3.5_flash \
  --question bladder_1.2-Q6f9dd68e \
  --max-parallel 1
```

Run a full benchmark:

```bash
uv run clingen-bench eval \
  --agent "bash adapters/antigravity_gemini_3.5_flash/run.sh" \
  --agent-name antigravity_gemini_3.5_flash \
  --cohort all \
  --max-parallel 4
```

## Notes

- Antigravity print mode uses the model selected in Antigravity settings. Confirm
  it is Gemini 3.5 Flash before a run.
- The adapter adds both the cohort directory and scratch directory with
  `--add-dir`, runs from scratch, and tells the model to write intermediates only
  there.
- Cohort data may be sensitive. Only use this adapter with an Antigravity/Gemini
  configuration approved for the benchmark data.
