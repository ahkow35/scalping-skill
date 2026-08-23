import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import strip_btc


P = strip_btc.DEFAULT_PARAMS


def k(c):
    return {"o": c, "h": c, "l": c, "c": c, "v": 1.0}


def series(closes):
    return [k(c) for c in closes]


def c1h(closes):
    """Wrap a close list as a {'1h': [...]} candle dict."""
    return {"1h": series(closes)}


# ---------- primitive: recent_move_pct ----------

def test_recent_move_pct_basic():
    # 7 closes, lookback 6: base=closes[0]=100, last=103 -> +3%
    assert strip_btc.recent_move_pct(series([100, 100, 101, 101, 102, 102, 103]), 6) == 3.0


def test_recent_move_pct_negative_and_clamp():
    assert strip_btc.recent_move_pct(series([100, 98]), 6) == -2.0  # lookback clamps to 1


def test_recent_move_pct_guards():
    assert strip_btc.recent_move_pct([], 6) is None
    assert strip_btc.recent_move_pct(series([100]), 6) is None
    assert strip_btc.recent_move_pct(series([0, 5]), 6) is None  # base 0


# ---------- decision table (corr passed in to isolate the logic) ----------

def test_beta_cuts_one_tier():
    r = strip_btc.classify(c1h([100, 100, 101, 101, 102, 102, 103]),   # coin +3%
                           c1h([100, 100, 101, 101, 102, 102, 103]),   # BTC  +3%
                           corr=0.8)
    assert r["status"] == "beta"
    assert r["cut"] is True
    assert r["cut_tiers"] == 1


def test_idiosyncratic_outrunning_no_cut():
    # coin +5% while BTC +1% -> outruns (5 > 1.5*1) -> coin-specific (the live HYPE case)
    r = strip_btc.classify(c1h([100, 100, 100, 100, 100, 100, 105]),
                           c1h([100, 100, 100, 100, 100, 100, 101]),
                           corr=0.8)
    assert r["status"] == "idiosyncratic"
    assert r["cut"] is False


def test_idiosyncratic_opposing_no_cut():
    # coin up while BTC down -> not aligned -> coin-specific
    r = strip_btc.classify(c1h([100, 100, 100, 100, 100, 100, 103]),
                           c1h([100, 100, 100, 100, 100, 100, 98]),
                           corr=0.8)
    assert r["status"] == "idiosyncratic"
    assert r["cut"] is False


def test_btc_flat_no_cut():
    # BTC ~flat (+0.05% < min_btc_move 0.15) -> coin move can't be beta
    r = strip_btc.classify(c1h([100, 100, 100, 100, 100, 100, 103]),
                           c1h([100, 100, 100, 100, 100, 100, 100.05]),
                           corr=0.8)
    assert r["status"] == "idiosyncratic"
    assert r["cut"] is False
    assert "flat" in r["reason"]


def test_moderate_corr_no_cut():
    # corr 0.55 in [0.4, 0.7) -> not clearly BTC-driven (the live HYPE corr 0.52 band)
    r = strip_btc.classify(c1h([100, 100, 101, 101, 102, 102, 103]),
                           c1h([100, 100, 101, 101, 102, 102, 103]),
                           corr=0.55)
    assert r["status"] == "moderate"
    assert r["cut"] is False


def test_decoupled_no_cut():
    r = strip_btc.classify(c1h([100, 100, 101, 101, 102, 102, 103]),
                           c1h([100, 100, 101, 101, 102, 102, 103]),
                           corr=0.3)
    assert r["status"] == "decoupled"
    assert r["cut"] is False


def test_unavailable_when_no_data():
    r = strip_btc.classify({}, {}, corr=None)
    assert r["status"] == "unavailable"
    assert r["cut"] is False


def test_never_raises_on_junk():
    r = strip_btc.classify("junk", "junk", corr=None)
    assert r["status"] == "unavailable"
    assert r["cut"] is False


# ---------- corr computed internally (regime.btc_corr reuse) ----------

def test_corr_computed_from_15m_when_not_passed():
    # 33 identical 15m closes for coin and BTC -> perfectly correlated returns (corr 1.0)
    closes15 = list(range(100, 133))  # 33 values, monotone
    candles = {"15m": series(closes15), "1h": series([100, 100, 101, 101, 102, 102, 103])}
    btc = {"15m": series(closes15), "1h": series([100, 100, 101, 101, 102, 102, 103])}
    r = strip_btc.classify(candles, btc)  # corr not passed -> computed
    assert r["btc_corr"] == 1.0
    assert r["status"] == "beta"
    assert r["cut"] is True


def test_move_fields_populated():
    r = strip_btc.classify(c1h([100, 100, 100, 100, 100, 100, 102]),
                           c1h([100, 100, 100, 100, 100, 100, 101]),
                           corr=0.8)
    assert r["coin_move_pct"] == 2.0
    assert r["btc_move_pct"] == 1.0
    assert r["move_interval"] == "1h"
