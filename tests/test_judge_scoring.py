"""Tests for the two-LLM judge scale: yes/unable/no = 2/1/0 per judge, summed.

No network: both judges are stubbed with canned JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from clin_genomic_analysis_benchmark.scoring import judge
from clin_genomic_analysis_benchmark.scoring.types import JUDGE_MAX_PER_CONCEPT, label_points


@dataclass
class _Resp:
    text: str


class FakeClient:
    """Returns one canned answer per gold concept, in order."""

    def __init__(self, answers, concepts=None, raise_exc=None, raw=None):
        self.answers = answers
        self.concepts = concepts
        self.raise_exc = raise_exc
        self.raw = raw
        self.calls = 0

    def generate(self, *, system_text, user_text, max_tokens=None, **kw):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        if self.raw is not None:
            return _Resp(self.raw)
        names = self.concepts or [f"c{i}" for i in range(len(self.answers))]
        return _Resp(json.dumps({"verdicts": [
            {"gold_concept": n, "reasoning": "because", "answer": a}
            for n, a in zip(names, self.answers)
        ]}))


def _score(claude_answers, azure_answers, gold=("c0",), **kw):
    return judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=list(gold), agent_concepts=["something"],
        claude_client=FakeClient(claude_answers, list(gold)),
        azure_client=FakeClient(azure_answers, list(gold)),
        **kw)


# ---- the scale ------------------------------------------------------------

@pytest.mark.parametrize("label,pts", [
    ("yes", 2.0), ("no", 0.0), ("unable to determine", 1.0),
])
def test_label_points(label, pts):
    assert label_points(label) == pts


def test_both_yes_is_full_marks():
    res = _score(["yes"], ["yes"])
    assert res.points == 4.0 == JUDGE_MAX_PER_CONCEPT
    assert res.points_possible == 4.0


def test_both_no_is_zero():
    assert _score(["no"], ["no"]).points == 0.0


def test_split_lands_in_the_middle_without_a_tiebreak():
    res = _score(["yes"], ["no"])
    assert res.points == 2.0
    assert res.n_split == 1
    assert res.decisions[0].labels == {"claude": "yes", "azure": "no"}


def test_unable_is_worth_one_per_judge():
    assert _score(["unable to determine"], ["unable to determine"]).points == 2.0
    assert _score(["yes"], ["unable to determine"]).points == 3.0
    assert _score(["no"], ["unable to determine"]).points == 1.0


def test_points_are_summed_across_concepts():
    res = _score(["yes", "no", "unable to determine"],
                 ["yes", "no", "yes"],
                 gold=("c0", "c1", "c2"))
    assert res.points == 4.0 + 0.0 + 3.0
    assert res.points_possible == 12.0
    assert res.n_unable == 1


# ---- robustness -----------------------------------------------------------

def test_answer_synonyms_are_normalised():
    assert _score(["Yes."], ["TRUE"]).points == 4.0
    assert _score(["unable"], ["Unclear"]).points == 2.0


def test_unparseable_answer_counts_as_unable_and_is_reported():
    res = _score(["banana"], ["yes"])
    assert res.decisions[0].labels["claude"] == "unable to determine"
    assert res.points == 3.0
    assert res.n_missing_verdicts == 1


def test_judge_exception_does_not_abort_the_run():
    res = judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=["c0"], agent_concepts=["x"],
        claude_client=FakeClient([], raise_exc=RuntimeError("endpoint down")),
        azure_client=FakeClient(["yes"], ["c0"]))
    assert res.decisions[0].labels["claude"] == "unable to determine"
    assert res.points == 3.0
    assert res.n_missing_verdicts == 1


def test_non_json_response_counts_as_unable():
    res = judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=["c0"], agent_concepts=["x"],
        claude_client=FakeClient(None, raw="I'm afraid I can't do that"),
        azure_client=FakeClient(["no"], ["c0"]))
    assert res.points == 1.0


def test_wrong_verdict_count_falls_back_to_name_matching():
    gold = ["alpha concept", "beta concept"]
    # claude returns them out of order and drops nothing; azure returns only one
    claude = FakeClient(["no", "yes"], ["beta concept", "alpha concept"])
    azure = FakeClient(["yes"], ["beta concept"])
    res = judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=gold, agent_concepts=["x"],
        claude_client=claude, azure_client=azure)
    by_concept = {d.gold_concept: d for d in res.decisions}
    # positional pairing applies for claude (count matches), name for azure
    assert by_concept["beta concept"].labels["azure"] == "yes"
    assert by_concept["alpha concept"].labels["azure"] == "unable to determine"


# ---- short circuits -------------------------------------------------------

def test_no_agent_concepts_scores_zero_without_calling_judges():
    claude, azure = FakeClient(["yes"]), FakeClient(["yes"])
    res = judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=["c0"], agent_concepts=[],
        claude_client=claude, azure_client=azure)
    assert res.points == 0.0
    assert claude.calls == 0 and azure.calls == 0


def test_no_gold_concepts_scores_zero_without_calling_judges():
    claude, azure = FakeClient([]), FakeClient([])
    res = judge.score_disambiguation(
        question_id="q", cohort="co", question_text="t",
        gold_concepts=[], agent_concepts=["x"],
        claude_client=claude, azure_client=azure)
    assert res.points == 0.0 and res.points_possible == 0.0
    assert claude.calls == 0 and azure.calls == 0


def test_never_emits_a_tie_or_needs_review():
    for a in ("yes", "no", "unable to determine"):
        for b in ("yes", "no", "unable to determine"):
            res = _score([a], [b])
            assert 0.0 <= res.points <= 4.0
            assert all(d.labels for d in res.decisions)
