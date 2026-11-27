# Codex + a partner model on Vertex's Responses API

This adapter runs the Codex CLI against a partner model that Google Vertex
serves on its per-publisher OpenAI Responses endpoint. It was built for
Meta's Muse Ember (`meta/muse-ember-1.0`), which Vertex serves only through
that endpoint:

```
https://aiplatform.googleapis.com/v1/projects/<project>/locations/global/publishers/meta/v1/responses
```

The request names the bare model id (`muse-ember-1.0`).

Codex custom providers already speak the Responses API, so no protocol
translation is needed. Each Codex attempt starts a localhost-only
pass-through bridge (`responses_bridge.py`), which does the following:

- **Credentials:** it holds the Google access token, obtained with
  `gcloud auth print-access-token` and refreshed after 45 minutes or on a 401.
  Codex, inside the bubblewrap sandbox, gets only an ephemeral bridge key in
  `VERTEX_RESPONSES_BRIDGE_KEY`.
- **Forwarding:** it rewrites `model` to the bare id and otherwise forwards
  each request unchanged. Server-sent event streams are relayed line by line.
- **Retries:** it retries `429`, `5xx` and transport errors with backoff, and
  a `401` once with a fresh token. All retries happen before anything is
  relayed. Codex may re-send a stream that drops midway (`stream_max_retries=2`).
- **Audit:** it writes content-free request and response metadata (item and
  tool types, statuses, token usage) to each question's
  `adapter_audit/vertex_responses_bridge.analyze.<attempt>.jsonl`.

One Codex feature is turned off: Vertex rejects the hosted `web_search` tool
("The 'web_search' tool is not supported"), so the adapter sets
`web_search="disabled"`. No request fields are stripped
(`UNSUPPORTED_REQUEST_FIELDS` is empty). Vertex accepted everything else Codex
0.169 sends: `store: false`, `include: ["reasoning.encrypted_content"]`,
`prompt_cache_key`, `client_metadata`, replayed reasoning items, and the
`namespace` tool.

Codex has no model metadata for `muse-ember-1.0` and warns that it is using
fallback metadata. This is expected.

## Prerequisites

- `codex` and `gcloud` on `PATH`
- an active gcloud login with access to the model in the project
- the benchmark's usual `CLINGEN_DATA_ROOT` (and, if needed, `CLINGEN_GOLD_ROOT`)

## Run

```bash
export CLINGEN_DATA_ROOT=...
VERTEX_RESPONSES_PROJECT_ID=profile-notes \
VERTEX_RESPONSES_MODEL=meta/muse-ember-1.0 \
clingen-bench analyze \
  --agent "bash adapters/codex_vertex_responses/run.sh" \
  --agent-name codex_vertex_responses_muse_ember
```

Set the `VERTEX_RESPONSES_*` variables in the shell that launches
`clingen-bench`, so the run manifest records them and repairs restore them.

| Variable | Default | Meaning |
|---|---|---|
| `VERTEX_RESPONSES_PROJECT_ID` | required | Google Cloud project |
| `VERTEX_RESPONSES_MODEL` | required | `<publisher>/<model>` |
| `VERTEX_RESPONSES_LOCATION` | `global` | Vertex location |
| `VERTEX_RESPONSES_REASONING_EFFORT` | unset (model default) | `minimal`/`low`/`medium`/`high`; forced on every request |
| `VERTEX_RESPONSES_CONTEXT_WINDOW` | `1048576` | Codex's context window; Muse Ember's documented window |
| `VERTEX_RESPONSES_REQUEST_TIMEOUT_SECONDS` | `900` | per upstream request |
| `VERTEX_RESPONSES_MAX_RETRIES` | `6` | upstream attempts per request |
| `VERTEX_RESPONSES_RETRY_BASE_SECONDS` | `5` | backoff base |
| `VERTEX_RESPONSES_MAX_RETRY_SLEEP_SECONDS` | `60` | backoff cap |
| `VERTEX_RESPONSES_MAX_REQUESTS` | `256` | model requests per Codex attempt |

Without `VERTEX_RESPONSES_REASONING_EFFORT`, Codex sends no effort for this
unknown model, and the model's default applies.
