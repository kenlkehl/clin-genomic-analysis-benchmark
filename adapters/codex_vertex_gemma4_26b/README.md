# Codex + Gemma 4 26B on Vertex Agent Platform

This adapter runs the Codex CLI while using Google's experimental
`google/gemma-4-26b-a4b-it-maas` model on Vertex Agent Platform MaaS.

Codex custom providers currently require the OpenAI Responses API, while this
Gemma endpoint currently exposes OpenAI-compatible Chat Completions. The
adapter therefore starts a localhost-only protocol bridge for each Codex
invocation. The trusted bridge obtains a short-lived token with
`gcloud auth print-access-token`; Google credentials and the host gcloud home
are never mounted into the model-controlled sandbox.

Prerequisites:

- `codex` and `gcloud` on `PATH`
- `gcloud auth login` (or another active gcloud credential)
- access to Gemma 4 26B MaaS in the selected project
- the benchmark's normal BPC dataset and gold-root environment variables

Run the benchmark:

```bash
VERTEX_GEMMA_PROJECT_ID="$(gcloud config get-value project)" \
uv run clingen-bench eval \
  --agent "bash adapters/codex_vertex_gemma4_26b/run.sh" \
  --agent-name codex_vertex_gemma4_26b \
  --max-parallel 1
```

Set `VERTEX_GEMMA_PROJECT_ID` in the shell that launches `clingen-bench` (as
shown), rather than only in an adapter-local shell. This lets the harness write
the effective project into `manifest.json` and restore it during repairs.
The conservative parallelism is intentional for the experimental shared MaaS
endpoint; raise it only after observing stable capacity in your project.

The model card says thinking/reasoning control is unsupported, so this adapter
does not accept or pass `CODEX_REASONING_EFFORT`. The run manifest records
effort as unsupported.

Useful reliability controls are `VERTEX_GEMMA_MAX_RETRIES` (default `6`),
`VERTEX_GEMMA_RETRY_BASE_SECONDS` (default `5`), and
`VERTEX_GEMMA_REQUEST_TIMEOUT_SECONDS` (default `600`). The bridge retries
transient `429` and `5xx` responses and saves content-free request metadata in
each question's `adapter_audit/vertex_bridge.*.jsonl` file.
