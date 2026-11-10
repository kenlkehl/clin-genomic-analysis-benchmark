"""Skeleton adapter for clin-genomic-analysis-benchmark.

Agents run only the analyze task: implement `answer_analyze(question: dict) -> dict`.
The harness writes question.json and reads result.json. Both follow the JSON
schemas defined in clin_genomic_analysis_benchmark/agent/contract.py.

Usage:
    python3 adapter.py --question-file <path> --output <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def answer_analyze(question: dict) -> dict:
    """Compute the answer. REPLACE THIS IMPLEMENTATION."""
    raise NotImplementedError("template adapter — implement analyze")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = json.loads(args.question_file.read_text())
    result = answer_analyze(question)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
