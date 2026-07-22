# Codex GPT-5.4 adapter

This adapter evaluates the Codex CLI using the default model/provider from
`~/.codex/config.toml`. On this machine, that config currently selects Azure
and `gpt-5.4`.

Unlike the Unsloth Codex adapter, this one does not override provider settings
by default. It lets Codex load your normal user config, then refreshes
`AZURE_OPENAI_API_KEY` immediately before every `codex exec` attempt:

```bash
az account get-access-token \
  --resource=https://cognitiveservices.azure.com/ \
  --query accessToken \
  --output tsv
```

## Contract

The benchmark harness calls:

```bash
bash adapters/codex_gpt_5.4/run.sh \
  --question-file <abs question.json> \
  --output <abs result.json>
```

The adapter builds a prompt from `AGENT_INSTRUCTIONS.md` plus the stage payload,
runs `codex exec` non-interactively, reads Codex's final message via
`--output-last-message`, extracts the final JSON object, and writes it to
`result.json`.

## Defaults

| variable | default | purpose |
|---|---|---|
| `CODEX_BIN` | `codex` | Codex executable |
| `CODEX_REFRESH_AZURE_TOKEN` | `1` | refresh `AZURE_OPENAI_API_KEY` before each Codex attempt |
| `CODEX_AZ_TOKEN_ATTEMPTS` | `3` | retry count for `az account get-access-token` |
| `CODEX_AZ_TOKEN_RETRY_SLEEP_SECONDS` | `5` | delay between Azure token refresh attempts |
| `CODEX_MAX_ATTEMPTS` | `3` | retry count for transient Codex stream/service failures |
| `CODEX_RETRY_BASE_SECONDS` | `15` | linear backoff base for Codex retries |
| `CODEX_SAVE_ATTEMPT_LOGS` | `1` | save full per-attempt Codex stdout/stderr in scratch |
| `CODEX_EPHEMERAL` | `1` | pass `--ephemeral` to avoid saving many sessions |
| `CODEX_SANDBOX_MODE_CLASSIFY` | `read-only` | classify sandbox |
| `CODEX_SANDBOX_MODE_DISAMBIGUATE` | `read-only` | disambiguate sandbox |
| `CODEX_SANDBOX_MODE_ANALYZE` | `workspace-write` | analyze sandbox |
| `CODEX_SANDBOX_MODE` | unset | fallback sandbox for all stages |
| `CODEX_PROFILE` | unset | optional Codex profile override |
| `CODEX_MODEL` | unset | optional model override; leave unset to use `~/.codex/config.toml` |

## Smoke test

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_gpt_5.4/run.sh" \
  --agent-name codex_gpt_5.4 \
  --question bladder_1.2-Q6f9dd68e \
  --max-parallel 1
```

## Full run

```bash
uv run clingen-bench eval \
  --agent "bash adapters/codex_gpt_5.4/run.sh" \
  --agent-name codex_gpt_5.4 \
  --cohort all \
  --max-parallel 4
```

Then score the run:

```bash
uv run clingen-bench score --run 'codex_gpt_5.4/<run_id>'
```

## Notes

- The adapter adds the cohort directory with `--add-dir` and runs Codex from
  the per-question scratch directory.
- The prompt instructs Codex to treat the cohort directory as read-only.
- Token values are never printed by the adapter.
