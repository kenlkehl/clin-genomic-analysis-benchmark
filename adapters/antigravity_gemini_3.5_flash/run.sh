#!/usr/bin/env bash
# Google Antigravity CLI + Gemini 3.5 Flash adapter for clin-genomic-analysis-benchmark.
#
# Contract: invoked by the harness as
#   run.sh --question-file <abs question.json> --output <abs result.json>

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
    *) echo "antigravity adapter: unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$QFILE" || -z "$OUT" ]]; then
  echo "usage: run.sh --question-file PATH --output PATH" >&2
  exit 2
fi

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ADAPTER_DIR/../.." && pwd)"

# Load repo-root .env if present. Useful for AGY_* overrides.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

# Antigravity's installer typically places agy in ~/.local/bin and also keeps
# helper binaries under ~/.gemini/antigravity-cli/bin.
export PATH="$HOME/.local/bin:$HOME/.gemini/antigravity-cli/bin:$PATH"

exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
