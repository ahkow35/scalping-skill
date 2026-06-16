# tests/test_regime.py
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import regime


def c(o, h, l, cl):
    return {"o": o, "h": h, "l": l, "c": cl}


def test_true_ranges_uses_prev_close():
    candles = [c(100, 101, 99, 100), c(100, 103, 99, 102)]
    # bar0 has no prev close -> high-low = 2.0
    # bar1: max(103-99, |103-100|, |99-100|) = 4.0
    assert regime.true_ranges(candles) == [2.0, 4.0]


def test_range_compression_quiet_when_recent_smaller():
    # 20 wide bars (range 4) then 6 tight bars (range 1) -> ratio < 1
    wide = [c(100, 102, 98, 100) for _ in range(20)]
    tight = [c(100, 100.5, 99.5, 100) for _ in range(6)]
    ratio = regime.range_compression(wide + tight, recent_n=6, baseline_n=20)
    assert ratio < 0.5


def test_range_compression_none_when_insufficient_bars():
    assert regime.range_compression([c(100, 101, 99, 100)], recent_n=6, baseline_n=20) is None


def test_directionality_high_for_trend():
    # monotonic up: net move == sum of abs moves -> directionality ~1.0
    candles = [c(0, 0, 0, float(i)) for i in range(10)]
    assert regime.directionality(candles) > 0.95


def test_directionality_low_for_chop():
    # oscillating closes: net ~0, large path -> directionality ~0
    closes = [100, 102, 100, 102, 100, 102, 100, 102]
    candles = [c(0, 0, 0, x) for x in closes]
    assert regime.directionality(candles) < 0.2


def test_btc_corr_perfect_positive():
    # identical return shapes -> corr ~ +1
    prim = [c(0, 0, 0, x) for x in [100, 101, 102, 101, 103]]
    btc = [c(0, 0, 0, x) for x in [200, 202, 204, 202, 206]]
    assert regime.btc_corr(prim, btc, lookback=5) > 0.95


def test_btc_corr_none_when_too_short():
    prim = [c(0, 0, 0, 100), c(0, 0, 0, 101)]
    btc = [c(0, 0, 0, 200), c(0, 0, 0, 202)]
    assert regime.btc_corr(prim, btc, lookback=20) is None


def test_two_sided_flow_high_when_balanced():
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 52.0},
          "1h": {"buy_share_pct": 48.0}}
    assert regime.two_sided_flow(td) > 0.9


def test_two_sided_flow_low_when_one_sided():
    td = {"5m": {"buy_share_pct": 2.0}, "15m": {"buy_share_pct": 5.0},
          "1h": {"buy_share_pct": 0.0}}
    assert regime.two_sided_flow(td) < 0.1


def test_two_sided_flow_none_when_no_volume():
    td = {"5m": {"buy_share_pct": None}, "15m": {"buy_share_pct": None}}
    assert regime.two_sided_flow(td) is None


# cc(close, rng): a candle centered on `close` with total high-low range `rng`.


def cc(close, rng):
    return c(close, close + rng / 2, close - rng / 2, close)


def test_classify_trending_label():
    up = [cc(100 + i, 1.0) for i in range(48)]
    btc = [cc(200 + i, 1.0) for i in range(48)]
    td = {"5m": {"buy_share_pct": 95.0}, "15m": {"buy_share_pct": 92.0}}
    book = {"execution": {"microprice_dev_bps": 1.0}}
    out = regime.classify({"15m": up, "1h": up}, {"15m": btc}, td, book)
    assert out["regime_label"] == "trending"
    assert out["fade_ok"] is False


def test_classify_ranging_label_sets_fade_ok():
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 51.0},
          "1h": {"buy_share_pct": 49.0}}
    book = {"execution": {"microprice_dev_bps": 0.2}}
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "ranging"
    assert out["fade_ok"] is True


def test_classify_correlated_chop_label():
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200 + (i % 2), 1.0) for i in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": 0.2}}
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "correlated-chop"
    assert out["fade_ok"] is False


def test_classify_quiet_label_when_recent_compressed():
    wide = [cc(100 + (i % 2) * 3.0, 4.0) for i in range(42)]
    tight = [cc(100 + (i % 2) * 0.1, 0.2) for i in range(6)]
    series = wide + tight
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": 0.0}}
    out = regime.classify({"15m": series, "1h": series}, {"15m": btc}, td, book)
    assert out["regime_label"] == "quiet"
    assert out["fade_ok"] is False


def test_classify_fade_ok_false_when_book_leans_hard():
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0},
          "1h": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": -9.0}}
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "ranging"
    assert out["fade_ok"] is False


def test_classify_degrades_gracefully_on_missing_data():
    out = regime.classify({"15m": [], "1h": []}, {"15m": []}, "DATA UNAVAILABLE", {})
    assert out["regime_label"] == "unknown"
    assert out["fade_ok"] is False
