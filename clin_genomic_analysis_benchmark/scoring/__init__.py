"""Scoring (Phase E).

For each run:
  1. Score the assess stage: the agent's needs-clarification verdict for each
     of the four components is compared exactly with gold. The subtask score
     is the mean over components of balanced accuracy.
  2. Score analysis via per-answer-type discrepancy bands → 2/1/0 pts, over
     every gold-unambiguous question.
Aggregate per-cohort and across cohorts. Write scorecard.{json,md}.
"""
