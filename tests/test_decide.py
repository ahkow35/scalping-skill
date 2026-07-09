# tests/test_decide.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import decide


def c(h, l, cl, v=1000.0, t=None):
    return {"o": cl, "h": h, "l": l, "c": cl, "v": v, "t": t}


CLEAR_BEHAVIORAL = {"daily_stop": {"active": False}, "cooldown": {"active": False}}
PROFILE = {"equity": 5000.0, "phase": 1}

# A smooth 15m triangle wave (100<->110, ~1/bar) with a tested ceiling ~110.3
# and a deep tested floor ~99.7, then a small sweep on the last bar (high 110.4,
# close 109.2 back below the ceiling). Smooth => small ATR => tight stop => the
# failed-breakout clears the 2:1 net-R:R floor (net ~2.44).
def _tri_bars():
    bars = []
    for i in range(44):
        ph = i % 20
        base = 100 + ph if ph <= 10 else 100 + (20 - ph)  # up 10, down 10
        jit = 0.1 * (i // 20) if base >= 110 else 0.0      # break peak ties
        p = base + jit
        bars.append((round(p + 0.3, 3), round(p - 0.3, 3)))
    bars[-1] = (110.4, 109.4)  # small sweep above the ceiling
    return bars


SHORT_SETUP_BARS = _tri_bars()
SETUP_PX = 109.2
FLAT_BTC = [c(100, 100, 100, v=1.0) for _ in range(20)]


def _series(bars, last_close=None):
    out = []
    for i, (h, l) in enumerate(bars):
        cl = (h + l) / 2
        if last_close is not None and i == len(bars) - 1:
            cl = last_close
        out.append(c(h, l, cl, t=str(i)))
    return out


def _clean_flow(bias):
    return {"coverage_ok": True, "max_coverage_pct": 100.0, "aggressor_bias": bias,
            "avg_buy_share_pct": 30.0 if bias == "sellers" else 70.0,
            "volume_climax": None, "delta_divergence": None, "breakout_vol_ok": True}


def _market(candles15, *, mid, funding=1.25e-05, flow=None, can_clear=True,
            d_chg=None, weekend=False, btc=FLAT_BTC):
    return {
        "primary": "HYPE",
        "ctx": {"coin": "HYPE", "mid": mid, "funding": funding, "mark": mid,
                "prev_day_px": mid},
        "btc_ctx": {"mark": 100.0, "prev_day_px": 100.0},
        "candles": {"15m": candles15}, "btc_candles": {"15m": btc},
        "flow": flow, "macro_can_clear": can_clear,
        "btc_dominance": {"btc_d_24h_chg": d_chg},
        "session": {"utc": "2026-07-08 00:00 UTC", "weekend_window": weekend},
    }


NO_CLOSE = {"use_closed_only": False}


def test_halt_on_daily_stop():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=_clean_flow("sellers"))
    bstate = {"daily_stop": {"active": True, "reason": "-2.1R", "until_utc": "x"},
              "cooldown": {"active": False}}
    out = decide.decide("short", m, bstate, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "HALT"
    assert out["sizing"] is None


def test_veto_on_funding_extreme_long():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, funding=0.0004)
    out = decide.decide("long", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "VETOED"
    assert "crowded long" in out["reason"]


def test_no_trade_when_macro_cannot_clear():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, can_clear=False)
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "NO-TRADE"


def test_short_now_on_clean_setup():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=_clean_flow("sellers"))
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "SHORT-NOW"
    assert out["conviction"] == "high"
    assert out["trigger_used"] == "A"
    assert out["sizing"]["coins"] > 0
    assert out["sizing"]["risk_usdc"] == 25.0  # 5000 * 0.5% * 1.0


def test_low_coverage_caps_to_probe():
    blind = {"coverage_ok": False, "max_coverage_pct": 1.0, "aggressor_bias": "sellers",
             "volume_climax": None, "delta_divergence": None, "breakout_vol_ok": True}
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=blind)
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "SHORT-PROBE"
    assert out["conviction"] == "low"
    assert out["sizing"]["tier_mult"] == 0.25


def test_flow_opposes_cuts_two_tiers():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=_clean_flow("buyers"))
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    # sellers wanted, buyers present -> -2 tiers -> low -> PROBE
    assert out["verdict"] == "SHORT-PROBE"


def test_wait_when_no_trigger():
    # mid-range price, no sweep -> nothing fires
    flat = _series([(105, 100), (106, 101), (105, 100), (106, 101), (105, 100),
                    (106, 101), (105, 100), (106, 101), (105, 100), (106, 101)])
    m = _market(flat, mid=103.0, flow=_clean_flow("sellers"))
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE, params=NO_CLOSE)
    assert out["verdict"] == "WAIT"
    assert out["sizing"] is None


def test_closed_only_drops_forming_bar():
    # With use_closed_only default True the last (sweep) bar is dropped, so the
    # rejection is not yet confirmed -> WAIT instead of SHORT-NOW.
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=_clean_flow("sellers"))
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, PROFILE)  # default params
    assert out["verdict"] == "WAIT"


def test_equity_unset_still_decides_without_sizing():
    m = _market(_series(SHORT_SETUP_BARS, SETUP_PX), mid=SETUP_PX, flow=_clean_flow("sellers"))
    out = decide.decide("short", m, CLEAR_BEHAVIORAL, {"equity": None, "phase": 1}, params=NO_CLOSE)
    assert out["verdict"] == "SHORT-NOW"
    assert out["sizing"] is None
    assert "equity unset" in out["flags"]["sizing"]
