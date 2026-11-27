# Claude Code + Claude on the Databricks Unity AI Gateway

This adapter runs the Claude Code CLI against a Claude model served by the
Databricks Unity AI Gateway's Anthropic Messages route
(`{host}/ai-gateway/anthropic/v1/messages`).

Each invocation starts a localhost-only pass-through, `databricks_bridge.py`.
The bridge runs outside the sandbox and gets a fresh Databricks OAuth token
from `databricks auth token` (reused until five minutes before it expires).
Claude Code receives only an ephemeral bearer key for `127.0.0.1`, so no
Databricks credential enters the model-controlled sandbox. Request and
response bodies pass through unchanged except that:

- `model` is rewritten to the gateway endpoint name. Claude Code is told the
  plain Claude ID (`claude-haiku-5-5`), so its model-specific settings
  (thinking, effort, context window) are the ones it uses for that model.
- Features the gateway rejects under its HIPAA configuration are removed:
  the `context_management` request field and its `context-management-*` beta
  header. Claude Code sends them for tool-result clearing; without them, a
  long session relies on Claude Code's own compaction. Each removal is
  recorded in the audit file.
- The gateway has no token-counting route, so `/v1/messages/count_tokens`
  returns a rough estimate.

Prerequisites:

- `claude` and `databricks` (the Databricks CLI) on `PATH`
- a Databricks CLI login for the gateway workspace, normally run once by the
  user: `databricks auth login --host <workspace-url> --profile unity-gateway`
- access to the gateway endpoint
- the benchmark's normal BPC data and gold-root environment variables

Run the benchmark:

```bash
DATABRICKS_CLAUDE_ENDPOINT=ai_gateway_model_services_prod.<team>_<project>.<team>_<project>-claude-haiku-5-5 \
CLINGEN_CLAUDE_EFFORT=medium \
uv run clingen-bench analyze \
  --agent "bash adapters/claude_code_databricks/run.sh" \
  --agent-name claude_code_databricks_haiku55_medium
```

Settings (set them in the shell that launches `clingen-bench` so the manifest
records them and repairs restore them):

| Variable | Meaning |
|---|---|
| `DATABRICKS_CLAUDE_ENDPOINT` | Required. The full gateway endpoint name. |
| `CLINGEN_CLAUDE_MODEL` | The Claude ID Claude Code is told. Default: the `claude-*` suffix of the endpoint name. |
| `CLINGEN_CLAUDE_EFFORT` | Passed to `claude --effort`. Unset means Claude Code's default (medium for Haiku and Opus 5.5 in November 2026). |
| `DATABRICKS_CONFIG_PROFILE` | Databricks CLI profile. Default `unity-gateway`. |
| `DATABRICKS_CLAUDE_REQUEST_TIMEOUT_SECONDS` | Per upstream call. Default 900. |
| `DATABRICKS_CLAUDE_MAX_REQUESTS` | Model calls allowed per question. Default 500. |

Each question's `adapter_audit/databricks_bridge.analyze.jsonl` records, per
model call, the request size, effort, thinking mode, removed features, the
HTTP status and the token usage (including prompt-cache reads), and the
totals for the question. It records no prompt or response content.

The gateway's Message Batches API is unavailable, and calls are billed to the
team's gateway allocation.
