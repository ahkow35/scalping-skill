import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flow


P = flow.DEFAULT_PARAMS


def k(o, c, v, h=None, l=None):
    h = h if h is not None else max(o, c)
    l = l if l is not None else min(o, c)
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def td(**windows):
    """Build a taker_delta dict: td(**{'5m': (buy_share, coverage)})."""
    out = {}
    for w, (bs, cov) in windows.items():
        out[w] = {"buy_share_pct": bs, "coverage_pct": cov, "delta_usdc": 0.0}
    return out


# ---------- primitives ----------

def test_max_coverage_pct():
    assert flow.max_coverage_pct(td(**{"5m": (60, 20.0), "15m": (55, 80.0)})) == 80.0
    assert flow.max_coverage_pct({}) is None
    assert flow.max_coverage_pct("DATA UNAVAILABLE") is None


def test_aggressor_bias():
    assert flow.aggressor_bias(td(**{"5m": (70, 90), "15m": (65, 90)}), P)[0] == "buyers"
    assert flow.aggressor_bias(td(**{"5m": (30, 90), "15m": (35, 90)}), P)[0] == "sellers"
    assert flow.aggressor_bias(td(**{"5m": (50, 90), "15m": (51, 90)}), P)[0] == "balanced"
    assert flow.aggressor_bias({}, P) == (None, None)


def test_volume_climax_down_and_up():
    base = [k(100, 100, 100) for _ in range(20)]
    down = base + [k(100, 90, 1000)]   # 10x, close < open
    res = flow.volume_climax(down, P)
    assert res["direction"] == "down" and res["ratio"] >= 8.0
    up = base + [k(100, 110, 1000)]
    assert flow.volume_climax(up, P)["direction"] == "up"


def test_volume_climax_none_when_no_spike_or_too_few_bars():
    base = [k(100, 100, 100) for _ in range(20)]
    assert flow.volume_climax(base + [k(100, 101, 300)], P) is None  # 3x < 8x
    assert flow.volume_climax([k(100, 101, 1000)], P) is None        # too few bars


def test_climax_and_breakout_adapt_to_short_history():
    # fetch_market returns ~19 5m bars — fewer than the 20-bar baseline. The
    # signals must still compute off what's available (down to min_bars floor).
    base = [k(100, 100, 100) for _ in range(13)]   # 13 prior bars (> min_bars 12)
    assert flow.volume_climax(base + [k(100, 90, 1000)], P)["direction"] == "down"
    assert flow.breakout_vol_ok(base + [k(100, 101, 200)], P) is True
    # below the floor => not computable
    short = [k(100, 100, 100) for _ in range(8)]
    assert flow.volume_climax(short + [k(100, 90, 1000)], P) is None
    assert flow.breakout_vol_ok(short + [k(100, 101, 200)], P) is None


def test_price_direction():
    up = [k(100, 100 + i, 1) for i in range(8)]
    assert flow.price_direction(up, 6) == "up"
    down = [k(100, 100 - i, 1) for i in range(8)]
    assert flow.price_direction(down, 6) == "down"
    assert flow.price_direction([k(100, 100, 1)], 6) is None


def test_delta_divergence():
    up = [k(100, 100 + i, 1) for i in range(8)]          # price up
    bearish = flow.delta_divergence(up, td(**{"5m": (30, 90)}), P)  # sellers aggressive
    assert bearish == "bearish"
    down = [k(100, 100 - i, 1) for i in range(8)]        # price down
    bullish = flow.delta_divergence(down, td(**{"5m": (70, 90)}), P)  # buyers aggressive
    assert bullish == "bullish"
    # agreement = no divergence
    assert flow.delta_divergence(up, td(**{"5m": (70, 90)}), P) is None


def test_breakout_vol_ok():
    base = [k(100, 100, 100) for _ in range(20)]
    assert flow.breakout_vol_ok(base + [k(100, 101, 200)], P) is True   # 2x
    assert flow.breakout_vol_ok(base + [k(100, 101, 120)], P) is False  # 1.2x
    assert flow.breakout_vol_ok([k(100, 101, 999)], P) is None          # too few


# ---------- classify composition ----------

def test_classify_full():
    base = [k(100, 100 + i * 0.1, 100) for i in range(20)]
    candles = {"5m": base + [k(102, 102.5, 250)]}  # up move, 2.5x vol breakout
    taker = td(**{"5m": (30, 80), "15m": (40, 80)})  # sellers despite price up
    f = flow.classify(candles, taker)
    assert f["coverage_ok"] is True
    assert f["aggressor_bias"] == "sellers"
    assert f["delta_divergence"] == "bearish"   # price up on selling
    assert f["breakout_vol_ok"] is True
    assert f["interval"] == "5m"


def test_classify_low_coverage_flags_not_ok():
    f = flow.classify({"5m": []}, td(**{"5m": (60, 10.0)}))
    assert f["coverage_ok"] is False
    assert f["max_coverage_pct"] == 10.0


def test_classify_never_raises_on_garbage():
    assert flow.classify("nope", "DATA UNAVAILABLE")["coverage_ok"] is False
    assert flow.classify({}, {})["aggressor_bias"] is None
    assert flow.classify(None, None)["volume_climax"] is None
