"""Scoring (Phase E).

For each run:
  1. Score classification (per-question 0/1).
  2. Score disambiguation via dual-LLM judge (Claude/Vertex + Azure OpenAI);
     disagreements queued for manual adjudication.
  3. Score analysis via per-answer-type discrepancy bands → 2/1/0 pts.
Aggregate per-cohort and across cohorts. Write scorecard.{json,md}.
"""
