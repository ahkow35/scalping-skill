"""Deterministic trigger evaluation for the scalp skill.

Turns structure.py levels into concrete entries. Each trigger is a PURE function
of the recent candle window plus the structure read — no persistent state
machine. This mirrors how the skill already re-derives everything from candles
on each call, and keeps the loop stateless.

Triggers (from scalp-long.md / scalp-short.md):
  long  A  sweep-reclaim     — stop-run below a tested floor, closes back above
  long  B  momentum-break    — closes above a tested ceiling (fresh cross)
  short A  failed-breakout   — sweeps above a tested ceiling, closes back below
  short B  lower-high reject  — makes a lower swing high and rejects

Each returns entry / stop / T1 / T2 with gross + net R:R from costs.py. The
NET-R:R admission floor (Step 6c) is reported as `passes_rr`; the fire/wait and
sizing decision belongs to decide.py, not here. Volume/flow confirmation stays
in flow.py (Step 1b) — triggers are pure price structure. Never raises.
"""

import costs
import structure


DEFAULT_PARAMS = {
    "stop_buffer_atr": 0.25,  # buffer beyond the structural stop level
    "min_net_rr_t1": 2.0,     # Step 6c net-R:R floor on T1
    "sweep_lookback": 3,      # recent bars scanned for the sweep wick
    "confirm_close": True,    # require the latest close to confirm the pattern
    "entry_mode": "close",    # "close" = market at signal-bar close;
                              # "retest" = limit at the level (fills only on retest)
}


# ---- geometry helpers ----

def _floors(s):
    return [l for l in s.get("floors", [])]


def _ceilings(s):
    return [l for l in s.get("ceilings", [])]


def _all_levels(s):
    return sorted(_ceilings(s) + _floors(s), key=lambda x: x["price"])


def _nearest_below(levels, price):
    below = [l for l in levels if l["price"] < price]
    return max(below, key=lambda x: x["price"], default=None)


def _nearest_above(levels, price):
    above = [l for l in levels if l["price"] > price]
    return min(above, key=lambda x: x["price"], default=None)


def _targets(side, s, entry):
    """First two structural targets beyond entry in the trade direction, drawn
    from the mid pivot + same-direction tested levels."""
    cands = set()
    if s.get("mid") is not None:
        cands.add(s["mid"])
    for l in (_ceilings(s) if side == "long" else _floors(s)):
        cands.add(l["price"])
    if side == "long":
        xs = sorted(x for x in cands if x > entry)
    else:
        xs = sorted((x for x in cands if x < entry), reverse=True)
    return (xs[0] if xs else None, xs[1] if len(xs) > 1 else None)


def _buffer(s, p):
    return (s.get("atr") or 0.0) * p["stop_buffer_atr"]


def _last_close(candles):
    return float(candles[-1]["c"]) if candles else None


def _prev_close(candles):
    return float(candles[-2]["c"]) if len(candles) >= 2 else _last_close(candles)


def _mk(side, name, entry, stop, t1, t2, reason, p, fired, entry_mode="close"):
    """Build a trigger result, validating trade geometry and R:R."""
    blank = {
        "trigger": name, "fired": False, "entry": entry, "stop": stop,
        "t1": t1, "t2": t2, "rr_t1": 0.0, "rr_t2": 0.0,
        "net_rr_t1": 0.0, "net_rr_t2": 0.0, "passes_rr": False,
        "entry_mode": entry_mode, "reason": reason,
    }
    if fired and t1 is None and entry is not None and stop is not None:
        blank["reason"] = "pattern present but no structural target beyond entry"
        return blank
    if not fired or entry is None or stop is None or t1 is None:
        return blank
    ok_geometry = (stop < entry < t1) if side == "long" else (t1 < entry < stop)
    if not ok_geometry:
        blank["reason"] = "bad geometry (entry/stop/target ordering)"
        return blank
    rr_t1 = costs.gross_rr(entry, stop, t1)
    net_t1 = costs.net_rr(entry, stop, t1)
    rr_t2 = costs.gross_rr(entry, stop, t2) if t2 is not None else 0.0
    net_t2 = costs.net_rr(entry, stop, t2) if t2 is not None else 0.0
    return {
        "trigger": name, "fired": True, "entry": round(entry, 6), "stop": round(stop, 6),
        "t1": round(t1, 6), "t2": round(t2, 6) if t2 is not None else None,
        "rr_t1": round(rr_t1, 2), "rr_t2": round(rr_t2, 2),
        "net_rr_t1": round(net_t1, 2), "net_rr_t2": round(net_t2, 2),
        "passes_rr": net_t1 >= p["min_net_rr_t1"],
        "entry_mode": entry_mode, "reason": reason,
    }


def _entry(p, px, level):
    """close-mode enters at the signal-bar close; retest-mode enters at the
    structural level (a limit that only fills if price returns to it)."""
    return px if p["entry_mode"] == "close" else level


# ---- long triggers ----

def long_a_sweep_reclaim(s, candles, p):
    floor = s.get("floor_below")
    if not floor or not s.get("sweep_reclaim"):
        return _mk("long", "long_A", None, None, None, None, "no floor sweep-reclaim", p, False)
    px = s["price"]
    if p["confirm_close"] and _last_close(candles) <= floor["price"]:
        return _mk("long", "long_A", None, None, None, None, "close not back above floor", p, False)
    # Faithful stop: BELOW the next structural pool below the swept floor (any
    # tested level). No pool -> no structural stop -> skip (never noise-tight).
    pool = _nearest_below(_all_levels(s), floor["price"])
    if not pool:
        return _mk("long", "long_A", None, None, None, None,
                   "no structural pool below floor for stop", p, False)
    stop = pool["price"] - _buffer(s, p)
    t1, t2 = _targets("long", s, px)
    entry = _entry(p, px, floor["price"])  # retest = the reclaimed floor
    return _mk("long", "long_A", entry, stop, t1, t2,
               "sweep+reclaim of tested floor", p, True, entry_mode=p["entry_mode"])


def long_b_momentum_break(s, candles, p):
    px = s["price"]
    # Momentum-break is a CEILING break only (a floor recapture is not a break).
    broken = _nearest_below(_ceilings(s), px)
    if not broken:
        return _mk("long", "long_B", None, None, None, None, "no tested ceiling below to break", p, False)
    fresh = _prev_close(candles) <= broken["price"] < _last_close(candles)
    if not fresh:
        return _mk("long", "long_B", None, None, None, None, "no fresh close above ceiling", p, False)
    stop = broken["price"] - _buffer(s, p)
    t1, t2 = _targets("long", s, px)
    entry = _entry(p, px, broken["price"])  # retest = broken ceiling as support
    return _mk("long", "long_B", entry, stop, t1, t2,
               "fresh close above tested ceiling", p, True, entry_mode=p["entry_mode"])


# ---- short triggers ----

def short_a_failed_breakout(s, candles, p):
    ceil = s.get("ceiling_above")
    if not ceil or not s.get("sweep_rejection"):
        return _mk("short", "short_A", None, None, None, None, "no ceiling sweep-rejection", p, False)
    px = s["price"]
    if p["confirm_close"] and _last_close(candles) >= ceil["price"]:
        return _mk("short", "short_A", None, None, None, None, "close not back below ceiling", p, False)
    highs = [float(k["h"]) for k in candles[-p["sweep_lookback"]:] if float(k["h"]) > ceil["price"]]
    ref = max(highs) if highs else ceil["price"]
    stop = ref + _buffer(s, p)
    t1, t2 = _targets("short", s, px)
    entry = _entry(p, px, ceil["price"])  # retest = the rejected ceiling from below
    return _mk("short", "short_A", entry, stop, t1, t2,
               "sweep+rejection of tested ceiling", p, True, entry_mode=p["entry_mode"])


def short_b_lower_high(s, candles, p):
    piv = structure.swing_pivots(candles)
    highs = [q for q in piv if q["kind"] == "high"]
    if len(highs) < 2:
        return _mk("short", "short_B", None, None, None, None, "need two swing highs", p, False)
    lower_high = highs[-1]
    if lower_high["price"] >= highs[-2]["price"]:
        return _mk("short", "short_B", None, None, None, None, "latest swing high not lower", p, False)
    px = s["price"]
    if p["confirm_close"] and _last_close(candles) >= lower_high["price"]:
        return _mk("short", "short_B", None, None, None, None, "no rejection below lower high", p, False)
    stop = lower_high["price"] + _buffer(s, p)
    t1, t2 = _targets("short", s, px)
    entry = _entry(p, px, lower_high["price"])  # retest = back up to the lower high
    return _mk("short", "short_B", entry, stop, t1, t2,
               "rejection at lower swing high", p, True, entry_mode=p["entry_mode"])


def evaluate(side, structure_out, candles, *, params=None):
    """Evaluate both triggers for a side. Returns {A: {...}, B: {...}} keyed by
    trigger letter (payload-friendly). Never raises."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    s = structure_out if isinstance(structure_out, dict) else {}
    if not candles or s.get("price") is None:
        blank = _mk(side, side + "_A", None, None, None, None, "no data", p, False)
        return {"A": blank, "B": dict(blank, trigger=side + "_B")}
    if side == "long":
        return {"A": long_a_sweep_reclaim(s, candles, p),
                "B": long_b_momentum_break(s, candles, p)}
    return {"A": short_a_failed_breakout(s, candles, p),
            "B": short_b_lower_high(s, candles, p)}
