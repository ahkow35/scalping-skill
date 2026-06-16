import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backtest_thresholds as bt


def c(t, o, h, l, cl):
    return {"t": t, "o": o, "h": h, "l": l, "c": cl}


H1 = 3_600_000
T0 = 1_750_000_000_000


def _flat(n, px=100.0, rng=1.0, start=T0):
    # n identical candles: range `rng`, close == open (no direction)
    return [c(start + i * H1, px, px + rng / 2, px - rng / 2, px)
            for i in range(n)]


def test_atr_veto_flags_spike_down():
    candles = _flat(20)
    # bar 20: range 3x the trailing average, closes hard down
    candles.append(c(T0 + 20 * H1, 100.0, 100.5, 97.5, 97.7))
    flags = bt.atr_veto_flags(candles, mult=2.0, lookback=20)
    assert flags[20] == "down"
    assert all(f is None for f in flags[:20])


def test_atr_veto_flags_spike_up_and_threshold_respected():
    candles = _flat(20)
    candles.append(c(T0 + 20 * H1, 100.0, 103.0, 99.9, 102.9))  # 3.1 range, up
    flags = bt.atr_veto_flags(candles, mult=2.0, lookback=20)
    assert flags[20] == "up"
    flags_strict = bt.atr_veto_flags(candles, mult=4.0, lookback=20)
    assert flags_strict[20] is None  # 3.1x below the 4x bar


def test_forward_stats_alignment():
    candles = [c(T0 + i * H1, 100, 101, 99, 100 + i) for i in range(6)]
    # closes: 100,101,102,103,104,105
    fwd = bt.forward_stats(candles, horizon_bars=2)
    assert fwd[0]["ret"] == pytest.approx((102 - 100) / 100)
    assert fwd[3]["ret"] == pytest.approx((105 - 103) / 103)
    assert fwd[4] is None and fwd[5] is None  # not enough future bars
    # MAE for a long: lowest low in the window vs entry close
    assert fwd[0]["mae_long"] == pytest.approx((99 - 100) / 100)
    # MAE for a short: highest high in the window vs entry close
    assert fwd[0]["mae_short"] == pytest.approx(-(101 - 100) / 100)


def test_align_funding_to_candles():
    candles = [c(T0, 100, 101, 99, 100), c(T0 + H1, 100, 101, 99, 100)]
    funding = [{"time": T0, "fundingRate": "0.0000125"},
               {"time": T0 + H1, "fundingRate": "0.0001"}]
    aligned = bt.align_funding(candles, funding)
    # hourly rate x 8 = per-8h equivalent (skill thresholds are per-8h)
    assert aligned[0] == pytest.approx(0.0001)
    assert aligned[1] == pytest.approx(0.0008)


def test_conditional_summary():
    rows = [
        {"ret": 0.01, "mae_long": -0.005},
        {"ret": -0.03, "mae_long": -0.04},
        {"ret": 0.02, "mae_long": -0.001},
    ]
    s = bt.summarize(rows, mae_key="mae_long")
    assert s["n"] == 3
    assert s["mean_ret_pct"] == pytest.approx(100 * (0.01 - 0.03 + 0.02) / 3)
    assert s["p_disaster"] == pytest.approx(1 / 3)  # ret < -2%
    assert s["mean_mae_pct"] == pytest.approx(100 * (-0.005 - 0.04 - 0.001) / 3)


def test_align_funding_tolerates_ms_offsets():
    # real HL fundingHistory times are off-hour by a few ms (e.g. ...000110)
    candles = [c(T0, 100, 101, 99, 100), c(T0 + H1, 100, 101, 99, 100)]
    funding = [{"time": T0 + 110, "fundingRate": "0.0000125"},
               {"time": T0 + H1 + 26, "fundingRate": "0.0001"}]
    aligned = bt.align_funding(candles, funding)
    assert aligned[0] == pytest.approx(0.0001)
    assert aligned[1] == pytest.approx(0.0008)


def test_regime_forward_outcomes_separates_ranging_from_trending():
    # Synthetic: a long ranging block (oscillation) then a clean trend block.
    # BTC is a SEPARATE flat series so btc_corr is undefined (None) -> the
    # correlated-chop rule is out of the way, and the structural gate sorts
    # ranging vs trending. Mean |forward return| must be LOWER after 'ranging'
    # bars than after 'trending' bars.
    import backtest_thresholds as bt
    ranging = []
    for i in range(60):
        px = 100.0 + (1.0 if i % 2 else -1.0)  # oscillate 99/101
        ranging.append(c(T0 + i * H1, px, px + 0.5, px - 0.5, px))
    trend = []
    for i in range(60):
        p = 101.0 + i  # clean up
        trend.append(c(T0 + (60 + i) * H1, p, p + 0.5, p - 0.5, p))
    series = ranging + trend
    btc_flat = [c(T0 + i * H1, 200.0, 200.05, 199.95, 200.0) for i in range(120)]
    stats = bt.regime_forward_stats(series, btc_flat, horizon_bars=4)
    assert stats["ranging"]["n"] > 0
    assert stats["trending"]["n"] > 0
    assert stats["trending"]["mean_abs_fwd_ret"] > stats["ranging"]["mean_abs_fwd_ret"]
