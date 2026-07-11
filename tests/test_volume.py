# tests/test_volume.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import volume


def c(v, t=None):
    return {"o": 1, "h": 1, "l": 1, "c": 1, "v": v, "t": t}


def test_rvol_basic_ratio():
    # 20 prior bars at v=10, signal bar v=30 -> RVOL 3.0
    candles = [c(10) for _ in range(20)] + [c(30)]
    assert volume.rvol(candles, 20) == 3.0


def test_rvol_excludes_signal_bar_from_mean():
    # prior 20 bars average 10; signal bar itself is 1000 and must not be
    # folded into the mean.
    candles = [c(10) for _ in range(20)] + [c(1000)]
    assert volume.rvol(candles, 20) == 100.0


def test_rvol_uses_only_prior_20_bars():
    # 25 prior bars: first 5 at v=1000 (should be excluded by the 20-window),
    # last 20 at v=10 -> mean 10, signal v=20 -> RVOL 2.0
    candles = [c(1000) for _ in range(5)] + [c(10) for _ in range(20)] + [c(20)]
    assert volume.rvol(candles, 25) == 2.0


def test_rvol_zero_volume_window_is_none():
    candles = [c(0) for _ in range(20)] + [c(15)]
    assert volume.rvol(candles, 20) is None


def test_rvol_short_window_uses_available_prior_bars():
    # only 3 prior bars exist (index 3), not a full 20 -> still computes.
    candles = [c(10), c(10), c(10), c(40)]
    assert volume.rvol(candles, 3) == 4.0


def test_rvol_no_prior_bars_is_none():
    candles = [c(10)]
    assert volume.rvol(candles, 0) is None


def test_rvol_index_out_of_range_is_none():
    candles = [c(10), c(20)]
    assert volume.rvol(candles, 5) is None


def test_rvol_missing_volume_key_treated_as_zero():
    candles = [{"o": 1, "h": 1, "l": 1, "c": 1, "t": i} for i in range(20)]
    candles.append(c(50))
    assert volume.rvol(candles, 20) is None  # prior window all missing -> 0 mean
