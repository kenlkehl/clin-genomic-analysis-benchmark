"""Scoring (Phase E).

Assess and analyze runs are scored separately:
  - assess: the model's needs-clarification verdict for each of the four
    components is compared exactly with gold, and a question is right only if
    all four match. The score is balanced exact match: the mean of the
    exact-match rates on gold-clear and gold-ambiguous questions.
  - analyze: per-answer-type discrepancy bands → 2/1/0 pts, over every
    gold-unambiguous question.
Each aggregates per cohort and across cohorts and writes scorecard.{json,md};
``combine`` weights one run of each into the headline.
"""
