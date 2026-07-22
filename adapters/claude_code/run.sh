#!/usr/bin/env bash
# Reference adapter for clin-genomic-analysis-benchmark using the Claude Code CLI on Vertex.
#
# Contract: invoked by the harness as
#   run.sh --question-file <abs question.json> --output <abs result.json>
#
# Reads the question, builds a prompt for the requested stage, shells out to
# `claude --print --output-format json --add-dir <cohort_dir>`, parses the
# final assistant message, and writes a contract-compliant result.json.

set -euo pipefail

QFILE=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --question-file) QFILE="$2"; shift 2 ;;
    --output)        OUT="$2";    shift 2 ;;
    *) echo "claude_code adapter: unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$QFILE" || -z "$OUT" ]]; then
  echo "usage: run.sh --question-file PATH --output PATH" >&2
  exit 2
fi

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ADAPTER_DIR/adapter.py" --question-file "$QFILE" --output "$OUT"
