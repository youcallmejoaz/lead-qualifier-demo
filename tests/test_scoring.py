import pytest

from src.evidence import verify_assessment
from src.models import Verdict
from src.rubric import parse_rubric
from src.scoring import decide_verdict, score
from tests.conftest import seed_records


@pytest.mark.parametrize("offer,bottleneck,exclusions,expected", [
    (8.0, 7.5, [], Verdict.qualified),
    (6.5, 6.5, [], Verdict.qualified),         # threshold is inclusive
    (8.0, 7.5, ["Already has a team"], Verdict.rejected),  # exclusion beats strong pillars
    (3.9, 9.0, [], Verdict.rejected),          # clearly low offer
    (9.0, 0.0, [], Verdict.rejected),          # clearly low bottleneck
    (6.0, 8.0, [], Verdict.review),            # in between
    (4.0, 6.4, [], Verdict.review),
])
def test_verdict_rules(offer, bottleneck, exclusions, expected):
    verdict, reason = decide_verdict(offer, bottleneck, exclusions)
    assert verdict is expected
    assert reason


def _score(name, rubric, load_account, load_assessment):
    va = verify_assessment(load_assessment(name), load_account(name), rubric)
    return va, score(va, rubric)


def test_blend_math_for_qualified_account(rubric, load_account, load_assessment):
    _, r = _score("sample_qualified", rubric, load_account, load_assessment)
    o = r.offer_works
    # rubric: (7x0.8 + 8x0.9 + 6x0.8 + 6x0.9) = 23.0 of 27 pts -> 8.5 ; Claude 9 x 100% = 9.0
    assert (o.points, o.max_points, o.rubric_part, o.claude_part) == (23.0, 27, 8.5, 9.0)
    assert o.score == 8.8
    b = r.founder_bottleneck
    # Claude 9 x 8/9 verified = 8.0 ; rubric (7.2 + 7.2 + 4.8 + 4.0 + 6.3) = 29.5 of 34 pts = 8.7
    assert (b.points, b.claude_part, b.rubric_part, b.score) == (29.5, 8.0, 8.7, 8.3)
    assert "0.5 × 9 (Claude 9, 7/7 verified) + 0.5 × 8.5 (rubric: 23 of 27 pts) = 8.8" in o.math
    # total = (8.8x10 + 8.3x10 + 10x3 + 7x3) / 26 x 10 = 85.4
    assert r.total == 85.4
    assert r.total_math == "Total = (8.8×10 + 8.3×10 + 7×3 + 10×3) / 26 × 10 = 85.4"
    assert r.verdict is Verdict.qualified


def test_no_post_evidence_scores_zero(rubric, load_account, load_assessment):
    _, r = _score("sample_weak_offer", rubric, load_account, load_assessment)
    assert r.offer_works.score == 0.0
    assert "no verified evidence" in r.offer_works.math
    assert r.verdict is Verdict.rejected


def test_exclusion_rejects_systemised(rubric, load_account, load_assessment):
    _, r = _score("sample_systemised", rubric, load_account, load_assessment)
    assert r.exclusions_hit == ["Already has a team"]
    assert r.verdict is Verdict.rejected


def _reweighted(name, weight):
    rows = seed_records()
    for row in rows:
        if row["fields"]["Name"] == name:
            row["fields"]["Weight"] = weight
    return parse_rubric(rows)


def test_changing_a_trigger_weight_changes_scores(rubric, load_account, load_assessment):
    va, before = _score("sample_weak_offer", rubric, load_account, load_assessment)
    # an unmatched trigger matters less -> the matched evidence counts for more
    after = score(va, _reweighted("Founder does all delivery", 0))
    assert after.founder_bottleneck.max_points == before.founder_bottleneck.max_points - 7
    assert after.founder_bottleneck.score > before.founder_bottleneck.score
    assert after.total > before.total
    assert after.rubric_version != before.rubric_version


def test_raising_a_matched_trigger_weight_changes_rubric_points(rubric, load_account, load_assessment):
    va, before = _score("sample_qualified", rubric, load_account, load_assessment)
    after = score(va, _reweighted("Manual hand raiser loop", 10))
    assert after.founder_bottleneck.points == before.founder_bottleneck.points + 1.8
    assert "31.3 of 36 pts" in after.founder_bottleneck.math


def test_raising_a_general_weight_changes_the_total(rubric, load_account, load_assessment):
    va, before = _score("sample_weak_offer", rubric, load_account, load_assessment)
    after = score(va, _reweighted("Engagement rate", 10))
    assert after.total < before.total  # this account has weak engagement


def test_deactivated_trigger_no_longer_counts(rubric, load_account, load_assessment):
    va, before = _score("sample_qualified", rubric, load_account, load_assessment)
    rows = seed_records()
    for row in rows:
        if row["fields"]["Name"] == "Sold out launches":
            row["fields"]["Active"] = False
    after = score(va, parse_rubric(rows))
    assert "Sold out launches" not in [m.name for m in after.offer_works.matched]
