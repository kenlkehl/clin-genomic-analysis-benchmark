#!/usr/bin/env bash
# Codex + Qwen 3.6 27B UD-Q4_K_XL on local Unsloth Studio.
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
if ! command -v codex >/dev/null 2>&1; then
  export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"
fi

exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
