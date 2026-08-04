#!/usr/bin/env bash
# Codex CLI adapter with configurable provider, model, and reasoning effort.
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
    *) echo "codex_gpt adapter: unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$QFILE" || -z "$OUT" ]]; then
  echo "usage: run.sh --question-file PATH --output PATH" >&2
  exit 2
fi

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ADAPTER_DIR/../.." && pwd)"

# Load repo-root .env if present. Useful for CODEX_* overrides.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

# Ensure Codex is reachable on machines where it was installed under Linuxbrew.
if ! command -v codex >/dev/null 2>&1; then
  export PATH="/home/linuxbrew/.linuxbrew/bin:$HOME/.local/bin:$PATH"
fi

exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
