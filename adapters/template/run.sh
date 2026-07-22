#!/usr/bin/env bash
# Template adapter for clin-genomic-analysis-benchmark.
# Replace this with your agent's invocation logic. The harness calls:
#   ./run.sh --question-file <abs question.json> --output <abs result.json>
#
# See adapters/template/README.md for full contract details.

set -euo pipefail
ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ADAPTER_DIR/adapter.py" "$@"
