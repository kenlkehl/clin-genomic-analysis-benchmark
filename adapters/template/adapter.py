"""Skeleton adapter for clin-genomic-analysis-benchmark.

Implement `answer_<stage>(question: dict) -> dict` for each of the three stages.
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


def answer_classify(question: dict) -> dict:
    """Decide ambiguous vs unambiguous. REPLACE THIS IMPLEMENTATION."""
    raise NotImplementedError("template adapter — implement classify")


def answer_disambiguate(question: dict) -> dict:
    """List concepts needed to disambiguate. REPLACE THIS IMPLEMENTATION."""
    raise NotImplementedError("template adapter — implement disambiguate")


def answer_analyze(question: dict) -> dict:
    """Compute the answer. REPLACE THIS IMPLEMENTATION."""
    raise NotImplementedError("template adapter — implement analyze")


_DISPATCH = {
    "classify": answer_classify,
    "disambiguate": answer_disambiguate,
    "analyze": answer_analyze,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--question-file", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()

    question = json.loads(args.question_file.read_text())
    fn = _DISPATCH[question["stage"]]
    result = fn(question)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
