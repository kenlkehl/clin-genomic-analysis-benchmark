#!/usr/bin/env bash
# Codex CLI adapter for Gemma 4 31B on an OpenAI-compatible vLLM server.

set -euo pipefail

QFILE=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "usage: run.sh --question-file PATH --output PATH"
      exit 0
      ;;
    --question-file) QFILE="$2"; shift 2 ;;
    --output)        OUT="$2";    shift 2 ;;
    *) echo "codex_vllm_gemma4_31b adapter: unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$QFILE" || -z "$OUT" ]]; then
  echo "usage: run.sh --question-file PATH --output PATH" >&2
  exit 2
fi

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v codex >/dev/null 2>&1; then
  export PATH="/home/linuxbrew/.linuxbrew/bin:$HOME/.local/bin:$PATH"
fi

exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
