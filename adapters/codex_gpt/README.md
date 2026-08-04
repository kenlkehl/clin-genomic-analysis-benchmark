# Codex Azure/OpenAI adapter

This adapter evaluates the Codex CLI using either explicit environment overrides
or the default model/provider from `~/.codex/config.toml`. The directory name is
historical; `CODEX_MODEL` can select any model available from the provider.

It lets Codex load your normal user config, applies any explicit provider/model/
effort overrides, then refreshes
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
bash adapters/codex_gpt/run.sh \
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
| `CODEX_MODEL_PROVIDER` | unset | optional provider key, such as `azure` |
| `CODEX_MODEL` | unset | optional model override; leave unset to use `~/.codex/config.toml` |
| `CODEX_REASONING_EFFORT` | unset | optional reasoning effort, such as `xhigh` |

## Smoke test

```bash
CODEX_MODEL_PROVIDER=azure \
CODEX_MODEL=gpt-5.6-terra \
CODEX_REASONING_EFFORT=xhigh \
uv run clingen-bench eval \
  --agent "bash adapters/codex_gpt/run.sh" \
  --agent-name codex_azure_gpt_5_6_terra_xhigh \
  --question bladder_1.2-Q6f9dd68e \
  --max-parallel 1
```

## Full run

```bash
CODEX_MODEL_PROVIDER=azure \
CODEX_MODEL=gpt-5.6-terra \
CODEX_REASONING_EFFORT=xhigh \
uv run clingen-bench eval \
  --agent "bash adapters/codex_gpt/run.sh" \
  --agent-name codex_azure_gpt_5_6_terra_xhigh \
  --cohort all \
  --max-parallel 4
```

Then score the run:

```bash
uv run clingen-bench score --run 'codex_azure_gpt_5_6_terra_xhigh/<run_id>'
```

## Notes

- The adapter adds the cohort directory with `--add-dir` and runs Codex from
  the per-question scratch directory.
- Explicit provider, model, and effort values are recorded in `manifest.json`
  and displayed at the top of generated scorecards.
- The prompt instructs Codex to treat the cohort directory as read-only.
- Token values are never printed by the adapter.
