import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import costs
import replay
import triggers


def test_cost_r_matches_round_trip_formula():
    expected = costs.ROUND_TRIP_FILLS * costs.COST_PER_FILL * 100.0 / 2.0
    assert costs.cost_r(100.0, 98.0) == pytest.approx(expected)


def test_net_reward_r_preserves_price_stop_denominator():
    gross = costs.gross_rr(100.0, 98.0, 104.0)
    net = costs.net_reward_r(100.0, 98.0, 104.0)
    assert gross == pytest.approx(2.0)
    assert net == pytest.approx(gross - costs.cost_r(100.0, 98.0))


@pytest.mark.parametrize("stop,target", [(99.7, 100.6), (100.3, 99.4)])
def test_tight_stop_net_rr_includes_costs_in_both_reward_and_loss(stop, target):
    # A 30 bps stop and 60 bps target cost 13 bps round trip under this model.
    # The win is +1.567 price-risk R; stopped loss is -1.433 price-risk R.
    assert costs.loss_per_unit(100, stop) == pytest.approx(0.43)
    assert costs.net_reward_r(100, stop, target) == pytest.approx(0.47 / 0.30)
    assert costs.net_rr(100, stop, target) == pytest.approx(0.47 / 0.43)


def test_trigger_rejects_legacy_two_r_payoff_with_insufficient_after_cost_ratio():
    # Legacy net payoff 2.067R would pass a 2.0 floor; actual reward/loss is 1.442.
    out = triggers._mk("long", "long_B", 100, 99.7, 100.75, None,
                       "fresh break", triggers.DEFAULT_PARAMS, True)
    assert out["fired"] is True
    assert out["net_reward_r_t1"] == 2.07
    assert out["net_rr_t1"] == 1.44
    assert out["passes_rr"] is False
    assert out["net_rr_denominator"] == "price_stop_plus_round_trip_cost"


def test_costs_exceeding_target_keep_negative_reward():
    assert costs.net_rr(100, 99.7, 100.1) < 0


def test_zero_price_risk_does_not_produce_an_admissible_rr():
    assert costs.net_reward_r(100, 100, 102) == 0.0
    assert costs.net_rr(100, 100, 102) == 0.0


def test_execution_cost_r_adds_half_spread():
    base = costs.cost_r(100.0, 98.0)
    half_spread_r = (4.0 / 10000 / 2) * 100.0 / 2.0
    assert costs.execution_cost_r(100.0, 98.0, spread_bps=4.0) == pytest.approx(
        base + half_spread_r)


def test_replay_uses_shared_cost_model():
    lv = replay.trigger_r_levels(
        {"entry": 100.0, "stop": 98.0, "t1": 104.0}, "long")
    assert replay.round_trip_cost_r(lv) == pytest.approx(costs.cost_r(100.0, 98.0))
