# tests/test_triggers.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import structure
import triggers


def c(o, h, l, cl, t=None):
    return {"o": o, "h": h, "l": l, "c": cl, "t": t}


def _lvl(price, touches=2, kind="floor"):
    return {"price": price, "touches": touches, "kind": kind, "last_idx": 0, "last_t": None}


# ---- long A: sweep-reclaim ----

def test_long_a_fires_on_sweep_reclaim():
    s = {
        "price": 100.5, "atr": 1.0, "mid": 103.0,
        "floor_below": _lvl(100.0), "ceiling_above": _lvl(105.0, kind="ceiling"),
        "floors": [_lvl(100.0), _lvl(97.0)],
        "ceilings": [_lvl(105.0, kind="ceiling"), _lvl(108.0, kind="ceiling")],
        "sweep_reclaim": True, "sweep_rejection": False,
    }
    candles = [c(0, 101, 98, 100.5)]  # wicked to 98, closed 100.5 above the floor
    out = triggers.evaluate("long", s, candles)
    a = out["A"]
    assert a["fired"] is True
    assert a["entry"] == 100.5
    assert a["stop"] < 97.0        # below the next pool (97), with buffer
    assert a["t1"] == 103.0        # mid pivot is the first target above entry
    assert a["stop"] < a["entry"] < a["t1"]


def test_long_a_no_fire_without_sweep():
    s = {"price": 100.5, "atr": 1.0, "mid": 103.0, "floor_below": _lvl(100.0),
         "ceiling_above": _lvl(105.0, kind="ceiling"), "floors": [_lvl(100.0)],
         "ceilings": [_lvl(105.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": False}
    assert triggers.evaluate("long", s, [c(0, 101, 100, 100.5)])["A"]["fired"] is False


# ---- long B: momentum-break ----

def test_long_b_fires_on_fresh_break():
    s = {"price": 106.0, "atr": 1.0, "mid": 103.0, "floor_below": None,
         "ceiling_above": _lvl(108.0, kind="ceiling"),
         "floors": [_lvl(100.0), _lvl(97.0)],
         "ceilings": [_lvl(105.0, kind="ceiling"), _lvl(108.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": False}
    candles = [c(0, 105, 103, 104.0), c(0, 107, 104, 106.0)]  # prev 104 <=105 < 106
    b = triggers.evaluate("long", s, candles)["B"]
    assert b["fired"] is True
    assert b["entry"] == 106.0
    assert abs(b["stop"] - (105.0 - 0.25)) < 1e-9   # below the broken 105, buffer 0.25*ATR
    assert b["t1"] == 108.0


def test_long_b_no_fire_when_not_fresh():
    s = {"price": 106.0, "atr": 1.0, "mid": 103.0, "floor_below": None,
         "ceiling_above": _lvl(108.0, kind="ceiling"), "floors": [_lvl(100.0)],
         "ceilings": [_lvl(105.0, kind="ceiling"), _lvl(108.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": False}
    candles = [c(0, 107, 105, 106.5), c(0, 107, 104, 106.0)]  # already above 105 last bar
    assert triggers.evaluate("long", s, candles)["B"]["fired"] is False


# ---- short A: failed-breakout ----

def test_short_a_fires_on_sweep_rejection():
    s = {"price": 104.5, "atr": 1.0, "mid": 103.0,
         "floor_below": _lvl(100.0), "ceiling_above": _lvl(105.0, kind="ceiling"),
         "floors": [_lvl(100.0), _lvl(97.0)],
         "ceilings": [_lvl(105.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": True}
    candles = [c(0, 107, 104, 104.5)]  # wicked to 107 above 105, closed 104.5 below
    a = triggers.evaluate("short", s, candles)["A"]
    assert a["fired"] is True
    assert a["entry"] == 104.5
    assert a["stop"] > 107.0        # above the sweep overshoot wick
    assert a["t1"] == 103.0         # mid is first target below entry
    assert a["t1"] < a["entry"] < a["stop"]


# ---- short B: lower-high rejection ----

def test_short_b_fires_on_lower_high():
    # peak 110 at i2, lower peak 108 at i6, then close below 108
    bars = [(100, 99), (105, 100), (110, 105), (105, 100), (100, 99),
            (104, 100), (108, 103), (104, 100), (102, 99)]
    candles = [c(0, h, l, (h + l) / 2 if i != 8 else 100.0) for i, (h, l) in enumerate(bars)]
    s = {"price": 100.0, "atr": 1.0, "mid": 105.0, "floor_below": _lvl(99.0),
         "ceiling_above": _lvl(108.0, kind="ceiling"),
         "floors": [_lvl(99.0), _lvl(96.0)], "ceilings": [_lvl(108.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": False}
    b = triggers.evaluate("short", s, candles)["B"]
    assert b["fired"] is True
    assert b["stop"] > 108.0        # above the lower high
    assert b["t1"] < b["entry"] < b["stop"]


def test_short_b_no_fire_when_higher_high():
    bars = [(100, 99), (105, 100), (108, 105), (105, 100), (100, 99),
            (104, 100), (110, 103), (104, 100), (102, 99)]  # second peak higher
    candles = [c(0, h, l, (h + l) / 2) for h, l in bars]
    s = {"price": 106.0, "atr": 1.0, "mid": 105.0, "floor_below": _lvl(99.0),
         "ceiling_above": None, "floors": [_lvl(99.0)], "ceilings": [],
         "sweep_reclaim": False, "sweep_rejection": False}
    assert triggers.evaluate("short", s, candles)["B"]["fired"] is False


# ---- guards ----

def test_evaluate_no_data_returns_unfired_pair():
    out = triggers.evaluate("long", {}, [])
    assert out["A"]["fired"] is False and out["B"]["fired"] is False


def test_bad_geometry_does_not_fire():
    # sweep_reclaim true but stop would land above entry (degenerate levels)
    s = {"price": 100.0, "atr": 0.0, "mid": 99.0, "floor_below": _lvl(100.0),
         "ceiling_above": _lvl(101.0, kind="ceiling"), "floors": [_lvl(100.0)],
         "ceilings": [_lvl(101.0, kind="ceiling")],
         "sweep_reclaim": True, "sweep_rejection": False}
    # mid (99) is below entry(100) -> no valid long target above -> no fire
    a = triggers.evaluate("long", s, [c(0, 101, 99, 100.0)])["A"]
    assert a["fired"] is False


# ---- integration through structure.classify_structure ----

def test_integration_short_failed_breakout():
    bars = [
        (101, 100.0), (106, 101), (110.0, 105), (106, 101), (101, 100.0),
        (106, 101), (110.2, 105), (106, 101), (101, 99.8), (106, 101),
        (110.1, 105), (106, 101), (101, 100.0),
        (113.0, 108.0),  # sweep above the ~110 ceiling, closes back below
    ]
    candles = [c(0, h, l, (h + l) / 2 if i != 13 else 108.5, t=str(i))
               for i, (h, l) in enumerate(bars)]
    s = structure.classify_structure(candles, price=108.5)
    assert s["sweep_rejection"] is True
    a = triggers.evaluate("short", s, candles)["A"]
    assert a["fired"] is True
    assert a["stop"] > 110.1
    assert a["t1"] < 108.5 < a["stop"]


def test_no_target_reports_clear_reason():
    # lower-high pattern present but no tested floor below -> honest "no target"
    bars = [(100, 99), (105, 100), (110, 105), (105, 100), (100, 99),
            (104, 100), (108, 103), (104, 100), (102, 99)]
    candles = [c(0, h, l, (h + l) / 2 if i != 8 else 100.0) for i, (h, l) in enumerate(bars)]
    s = {"price": 100.0, "atr": 1.0, "mid": None, "floor_below": None,
         "ceiling_above": _lvl(108.0, kind="ceiling"), "floors": [], "ceilings": [],
         "sweep_reclaim": False, "sweep_rejection": False}
    b = triggers.evaluate("short", s, candles)["B"]
    assert b["fired"] is False
    assert "no structural target" in b["reason"]


# ---- reviewer faithfulness patch ----

def test_long_b_ignores_floor_recapture():
    # fresh close above a FLOOR (support recapture) must NOT fire momentum-break
    s = {"price": 101.0, "atr": 1.0, "mid": 103.0, "floor_below": _lvl(100.0),
         "ceiling_above": _lvl(108.0, kind="ceiling"),
         "floors": [_lvl(100.0)], "ceilings": [_lvl(108.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": False}
    candles = [c(0, 101, 99, 99.0), c(0, 102, 100, 101.0)]  # prev 99<=100<101 across the floor
    assert triggers.evaluate("long", s, candles)["B"]["fired"] is False


def test_long_a_skips_when_no_structural_pool():
    # sweep-reclaim present but nothing below the floor to place a structural stop
    s = {"price": 100.5, "atr": 1.0, "mid": 103.0, "floor_below": _lvl(100.0),
         "ceiling_above": _lvl(105.0, kind="ceiling"),
         "floors": [_lvl(100.0)], "ceilings": [_lvl(105.0, kind="ceiling")],
         "sweep_reclaim": True, "sweep_rejection": False}
    a = triggers.evaluate("long", s, [c(0, 101, 98, 100.5)])["A"]
    assert a["fired"] is False
    assert "no structural pool" in a["reason"]


def test_retest_entry_uses_the_level_not_the_close():
    s = {"price": 104.5, "atr": 1.0, "mid": 103.0,
         "floor_below": _lvl(100.0), "ceiling_above": _lvl(105.0, kind="ceiling"),
         "floors": [_lvl(100.0), _lvl(97.0)], "ceilings": [_lvl(105.0, kind="ceiling")],
         "sweep_reclaim": False, "sweep_rejection": True}
    candles = [c(0, 107, 104, 104.5)]
    close = triggers.evaluate("short", s, candles)["A"]
    retest = triggers.evaluate("short", s, candles, params={"entry_mode": "retest"})["A"]
    assert close["entry_mode"] == "close" and close["entry"] == 104.5
    assert retest["entry_mode"] == "retest" and retest["entry"] == 105.0  # the ceiling
    assert retest["t1"] < retest["entry"] < retest["stop"]  # geometry holds
