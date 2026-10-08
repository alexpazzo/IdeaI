"""Test della formula di scoring (§8.4): minimi, massimi e valori di confine."""

from __future__ import annotations

import dataclasses
import math

import pytest

from ideai.scoring import ScoreInputs, Weights, engagement, opportunity_score, quality

SATURATION = 5000


def make(
    *,
    feasibility=3,
    economics=3,
    competition=3,
    seed_score=None,
    seed_num_comments=None,
    seed_is_comment=False,
    quotes=0,
    monetization="unknown",
    confidence=0.0,
) -> ScoreInputs:
    return ScoreInputs(
        feasibility=feasibility,
        economics=economics,
        competition=competition,
        seed_score=seed_score,
        seed_num_comments=seed_num_comments,
        seed_is_comment=seed_is_comment,
        engagement_saturation=SATURATION,
        valid_evidence_quotes=quotes,
        monetization_model=monetization,
        confidence=confidence,
    )


def test_quality_bounds_and_midpoint():
    assert quality(1, 1, 5) == 0.0
    assert quality(5, 5, 1) == 1.0
    assert quality(3, 3, 3) == pytest.approx(0.5, abs=1e-12)


def test_minimum_case_is_three():
    # quality=0, engagement=0, evidence=0, economy=0.3, confidence=0 -> 3
    assert opportunity_score(make(feasibility=1, economics=1, competition=5)) == 3


def test_no_engagement_but_full_quality_evidence_economy_is_70():
    # 0.40*1 + 0.20*1 + 0.10*1 = 0.70
    assert (
        opportunity_score(
            make(feasibility=5, economics=5, competition=1, quotes=5, monetization="subscription")
        )
        == 70
    )


def test_maximum_case_is_100():
    assert (
        opportunity_score(
            make(
                feasibility=5,
                economics=5,
                competition=1,
                seed_score=100000,
                seed_num_comments=100000,
                quotes=5,
                monetization="subscription",
                confidence=1.0,
            )
        )
        == 100
    )


def test_engagement_zero_when_seed_score_missing():
    assert engagement(make(seed_score=None)) == 0.0
    assert opportunity_score(make(seed_score=None)) == opportunity_score(make())


def test_comment_seed_uses_score_only():
    inp_comment = make(seed_score=100, seed_num_comments=500, seed_is_comment=True)
    inp_post = make(seed_score=100, seed_num_comments=0, seed_is_comment=False)
    assert engagement(inp_comment) == engagement(inp_post)
    # un commento ignora num_comments: 100 + 2*500 saturerebbe diversamente
    assert engagement(make(seed_score=100, seed_num_comments=500, seed_is_comment=False)) > 0


def test_engagement_is_capped_at_one_and_monotonic():
    below = engagement(make(seed_score=100, seed_is_comment=True))
    above = engagement(make(seed_score=SATURATION * 10, seed_is_comment=True))
    assert 0 < below < 1.0
    assert above == 1.0
    assert below == pytest.approx(math.log1p(100) / math.log1p(SATURATION), rel=1e-12)


def test_engagement_saturation_is_per_source():
    small = dataclasses.replace(
        make(seed_score=100, seed_is_comment=True), engagement_saturation=100
    )
    large = make(seed_score=100, seed_is_comment=True)
    assert engagement(small) > engagement(large)


def test_evidence_saturates_at_five():
    scores = [opportunity_score(make(quotes=n)) for n in range(0, 8)]
    assert scores[5] == scores[6] == scores[7]
    assert scores[0] < scores[5]


def test_economy_penalty_is_0_3_for_unknown():
    unknown = opportunity_score(make(monetization="unknown"))
    known = opportunity_score(make(monetization="ads"))
    assert known == unknown + 7


def test_half_up_rounding_on_exact_half():
    # quality(3,3,3)=0.5 -> 0.40*0.5=0.20; economy 0.10; confidence 0.05*0.5=0.025
    # totale 0.325 -> 32.5 -> half-up -> 33
    inp = make(monetization="subscription", confidence=0.5)
    assert opportunity_score(inp) == 33


def test_custom_weights_override():
    inp = make(feasibility=1, economics=1, competition=5)
    zeroed = Weights(quality=0.0, engagement=0.0, evidence=0.0, economy=0.0, confidence=0.0)
    assert opportunity_score(inp, zeroed) == 0
    assert opportunity_score(inp) == 3
