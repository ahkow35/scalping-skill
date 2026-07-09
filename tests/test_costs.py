import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import costs
import replay


def test_cost_r_matches_round_trip_formula():
    expected = costs.ROUND_TRIP_FILLS * costs.COST_PER_FILL * 100.0 / 2.0
    assert costs.cost_r(100.0, 98.0) == pytest.approx(expected)


def test_net_rr_subtracts_cost_from_gross_rr():
    gross = costs.gross_rr(100.0, 98.0, 104.0)
    net = costs.net_rr(100.0, 98.0, 104.0)
    assert gross == pytest.approx(2.0)
    assert net == pytest.approx(gross - costs.cost_r(100.0, 98.0))


def test_execution_cost_r_adds_half_spread():
    base = costs.cost_r(100.0, 98.0)
    half_spread_r = (4.0 / 10000 / 2) * 100.0 / 2.0
    assert costs.execution_cost_r(100.0, 98.0, spread_bps=4.0) == pytest.approx(
        base + half_spread_r)


def test_replay_uses_shared_cost_model():
    lv = replay.trigger_r_levels(
        {"entry": 100.0, "stop": 98.0, "t1": 104.0}, "long")
    assert replay.round_trip_cost_r(lv) == pytest.approx(costs.cost_r(100.0, 98.0))
