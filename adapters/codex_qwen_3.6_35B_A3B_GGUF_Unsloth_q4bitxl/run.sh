#!/usr/bin/env bash
# Codex + Unsloth Studio (open-source model) adapter for clin-genomic-analysis-benchmark.
#
# Agent/harness combo: the OpenAI Codex CLI driving a local Unsloth Studio
# server serving unsloth/Qwen3.6-35B-A3B-MTP-GGUF on port 8888. Mirrors the
# interactive `codex --profile unsloth_api` setup.
#
# Contract: invoked by the harness as
#   run.sh --question-file <abs question.json> --output <abs result.json>

set -euo pipefail

QFILE=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --question-file) QFILE="$2"; shift 2 ;;
    --output)        OUT="$2";    shift 2 ;;
    *) echo "codex adapter: unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$QFILE" || -z "$OUT" ]]; then
  echo "usage: run.sh --question-file PATH --output PATH" >&2
  exit 2
fi

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ADAPTER_DIR/../.." && pwd)"

# Load repo-root .env (UNSLOTH_STUDIO_AUTH_TOKEN, API_TOKEN, CODEX_* overrides)
# if present — the harness reads os.environ directly and does not auto-load .env.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

# Ensure the codex CLI is reachable (Homebrew/Linuxbrew install path).
if ! command -v codex >/dev/null 2>&1; then
  export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"
fi

exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
