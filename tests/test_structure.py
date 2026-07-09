# tests/test_structure.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import structure


def c(o, h, l, cl, t=None):
    return {"o": o, "h": h, "l": l, "c": cl, "t": t}


# ---- swing_pivots ----

def test_swing_pivots_finds_high_and_low():
    # index 3 is a clear peak (h=110), index 6 a clear trough (l=90)
    highs = [100, 101, 102, 110, 103, 101, 95, 96, 97]
    lows = [99, 100, 101, 108, 100, 98, 90, 94, 95]
    candles = [c(0, highs[i], lows[i], highs[i]) for i in range(len(highs))]
    piv = structure.swing_pivots(candles, left=2, right=2)
    kinds = {(p["idx"], p["kind"]) for p in piv}
    assert (3, "high") in kinds
    assert (6, "low") in kinds


def test_swing_pivots_excludes_unconfirmed_recent_bars():
    # last `right` bars can never be pivots even if they are extreme
    candles = [c(0, 100, 99, 100) for _ in range(6)]
    candles.append(c(0, 999, 998, 999))  # extreme, but it's the last bar
    piv = structure.swing_pivots(candles, left=2, right=2)
    assert all(p["idx"] <= len(candles) - 1 - 2 for p in piv)


def test_swing_pivots_empty_when_too_short():
    assert structure.swing_pivots([c(0, 1, 0, 1)], left=2, right=2) == []


# ---- cluster_levels ----

def test_cluster_levels_merges_within_eps_and_counts_touches():
    piv = [
        {"idx": 1, "price": 100.0, "kind": "high", "t": "a"},
        {"idx": 5, "price": 100.4, "kind": "high", "t": "b"},
        {"idx": 9, "price": 100.2, "kind": "high", "t": "c"},
    ]
    levels = structure.cluster_levels(piv, "high", eps=0.5)
    assert len(levels) == 1
    lvl = levels[0]
    assert lvl["touches"] == 3
    assert lvl["kind"] == "ceiling"
    assert lvl["last_idx"] == 9
    assert abs(lvl["price"] - 100.2) < 0.11


def test_cluster_levels_splits_when_beyond_eps():
    piv = [
        {"idx": 1, "price": 100.0, "kind": "low", "t": "a"},
        {"idx": 5, "price": 105.0, "kind": "low", "t": "b"},
    ]
    levels = structure.cluster_levels(piv, "low", eps=0.5)
    assert len(levels) == 2
    assert all(l["kind"] == "floor" for l in levels)


# ---- detect_sweep ----

def test_detect_sweep_ceiling_rejection():
    candles = [c(0, 100, 99, 99.5), c(0, 101.5, 99, 99.8)]  # wick above 100, close below
    assert structure.detect_sweep(candles, 100.0, "ceiling", lookback=3) is True


def test_detect_sweep_floor_reclaim():
    candles = [c(0, 101, 100, 100.5), c(0, 101, 98.5, 100.2)]  # wick below 100, close above
    assert structure.detect_sweep(candles, 100.0, "floor", lookback=3) is True


def test_detect_sweep_false_when_clean_break():
    # closes above the ceiling -> a real break, not a rejection sweep
    candles = [c(0, 102, 100, 101.5)]
    assert structure.detect_sweep(candles, 100.0, "ceiling", lookback=3) is False


# ---- _trend ----

def test_trend_up_on_higher_highs_and_lows():
    piv = [
        {"price": 100, "kind": "high"}, {"price": 98, "kind": "low"},
        {"price": 104, "kind": "high"}, {"price": 101, "kind": "low"},
        {"price": 108, "kind": "high"}, {"price": 105, "kind": "low"},
    ]
    assert structure._trend(piv, n=2) == "up"


def test_trend_down_on_lower_highs_and_lows():
    piv = [
        {"price": 108, "kind": "high"}, {"price": 105, "kind": "low"},
        {"price": 104, "kind": "high"}, {"price": 101, "kind": "low"},
        {"price": 100, "kind": "high"}, {"price": 97, "kind": "low"},
    ]
    assert structure._trend(piv, n=2) == "down"


def test_trend_none_when_choppy():
    piv = [
        {"price": 100, "kind": "high"}, {"price": 98, "kind": "low"},
        {"price": 104, "kind": "high"}, {"price": 95, "kind": "low"},  # lower low breaks HL
    ]
    assert structure._trend(piv, n=2) is None


# ---- classify_structure (integration) ----

def _range_series():
    """Zigzag between a ~100 floor and ~110 ceiling. Peaks/troughs carry tiny
    variation (within an ATR band) so each is a strict fractal pivot while still
    clustering into one tested level — exactly how real tested levels look."""
    # (high, low) per bar; peaks at i2/i6/i10, troughs at i4/i8
    bars = [
        (101, 100.0), (106, 101), (110.0, 105), (106, 101), (101, 100.0),
        (106, 101), (110.2, 105), (106, 101), (101, 99.8), (106, 101),
        (110.1, 105), (106, 101), (101, 100.0),
    ]
    return [c(0, h, l, (h + l) / 2, t=str(i)) for i, (h, l) in enumerate(bars)]


def test_classify_structure_range_gives_floor_and_ceiling():
    out = structure.classify_structure(_range_series(), price=105.0)
    assert out["classification"] == "range"
    assert out["floor_below"] is not None
    assert out["ceiling_above"] is not None
    assert out["floor_below"]["touches"] >= 2
    assert out["ceiling_above"]["touches"] >= 2
    assert out["mid"] is not None


def test_classify_structure_breakout_above_top_ceiling():
    out = structure.classify_structure(_range_series(), price=120.0)
    assert out["classification"] == "breakout"


def test_classify_structure_breakdown_below_bottom_floor():
    out = structure.classify_structure(_range_series(), price=90.0)
    assert out["classification"] == "breakdown"


def test_classify_structure_unknown_when_insufficient():
    out = structure.classify_structure([c(0, 1, 0, 1)], price=1.0)
    assert out["classification"] == "unknown"
    assert out["ceilings"] == []
    assert out["floor_below"] is None


def test_classify_structure_never_raises_on_empty():
    out = structure.classify_structure([], price=None)
    assert out["classification"] == "unknown"
    assert out["price"] is None


def test_classify_structure_flags_ceiling_sweep_rejection():
    series = _range_series()
    # append a bar that wicks above the ~110 ceiling but closes back below
    series.append(c(0, 113.0, 108.0, 108.5, t="sweep"))
    out = structure.classify_structure(series, price=108.5)
    assert out["sweep_rejection"] is True
