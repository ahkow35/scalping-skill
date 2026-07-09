"""Deterministic price-structure engine for the scalp skill.

Ports the LLM's "where are the floors/ceilings, is this a sweep" judgement into
pure functions over candles. No I/O, no network — decide.py calls
classify_structure() and the trigger state machine (triggers.py) consumes the
levels. Mirrors regime.py: data layer computes, callers read.

Method (all price-based, volume confirmation lives in flow.py / Step 1b):
  1. swing_pivots  — n-bar fractal pivots (Williams). The most recent `right`
     bars are NOT yet confirmable as pivots (need future bars) — deliberately
     excluded, matching "wait for confirmation".
  2. cluster_levels — group same-kind pivots within an ATR-scaled band; a level
     with >= min_touches is "tested".
  3. classify_structure — nearest tested ceiling/floor around price, mid pivot,
     structure label, and sweep-reclaim / sweep-rejection flags on those levels.

Never raises; every output is JSON-serialisable.
"""

import regime  # reuse the canonical Wilder true-range impl (DRY)


# Initial CANDIDATE thresholds — confirmed / retuned by the strategy backtest
# (PLAN-live-bot Phase 3). Do not treat as final until the plateau sweep passes.
DEFAULT_PARAMS = {
    "pivot_left": 2,      # bars to the left that a pivot must exceed
    "pivot_right": 2,     # bars to the right (=> last `right` bars unconfirmed)
    "atr_n": 14,          # ATR window for the cluster band
    "eps_atr_mult": 0.5,  # cluster band width = eps_atr_mult * ATR
    "min_touches": 2,     # touches for a level to count as "tested"
    "sweep_lookback": 3,  # recent bars scanned for a sweep of a level
    "trend_swings": 2,    # consecutive HH/HL (or LH/LL) to call a trend
}


def atr(candles, n=14):
    """Simple average true range over the last n bars. None if no bars."""
    trs = regime.true_ranges(candles)
    if not trs:
        return None
    window = trs[-n:] if len(trs) >= n else trs
    return sum(window) / len(window)


def swing_pivots(candles, left=2, right=2):
    """N-bar fractal pivots. A swing high at i requires high[i] strictly above
    every high in the `left` bars before and `right` bars after; swing low is
    symmetric on lows. Returns [{idx, price, kind: 'high'|'low', t}, ...] in
    index order. The last `right` bars are never pivots (unconfirmed)."""
    n = len(candles)
    if left < 1 or right < 1 or n < left + right + 1:
        return []
    highs = [float(k["h"]) for k in candles]
    lows = [float(k["l"]) for k in candles]
    out = []
    for i in range(left, n - right):
        if highs[i] > max(highs[i - left:i]) and highs[i] > max(highs[i + 1:i + right + 1]):
            out.append({"idx": i, "price": highs[i], "kind": "high", "t": candles[i].get("t")})
        if lows[i] < min(lows[i - left:i]) and lows[i] < min(lows[i + 1:i + right + 1]):
            out.append({"idx": i, "price": lows[i], "kind": "low", "t": candles[i].get("t")})
    return out


def cluster_levels(pivots, kind, eps):
    """Greedy-cluster same-kind pivots whose prices fall within `eps` of the
    running cluster mean. `kind` is the pivot kind ('high' -> ceiling, 'low' ->
    floor). Returns [{price, touches, kind, last_idx, last_t}, ...] sorted by
    price ascending. eps <= 0 clusters only exactly-equal prices."""
    pts = sorted((p for p in pivots if p["kind"] == kind), key=lambda x: x["price"])
    clusters = []
    for p in pts:
        if clusters and abs(p["price"] - clusters[-1]["_ref"]) <= eps:
            cl = clusters[-1]
            cl["prices"].append(p["price"])
            cl["members"].append(p)
            cl["_ref"] = sum(cl["prices"]) / len(cl["prices"])
        else:
            clusters.append({"_ref": p["price"], "prices": [p["price"]], "members": [p]})
    out = []
    for cl in clusters:
        last = max(cl["members"], key=lambda m: m["idx"])
        out.append({
            "price": round(sum(cl["prices"]) / len(cl["prices"]), 6),
            "touches": len(cl["prices"]),
            "kind": "ceiling" if kind == "high" else "floor",
            "last_idx": last["idx"],
            "last_t": last["t"],
        })
    out.sort(key=lambda x: x["price"])
    return out


def detect_sweep(candles, level_price, kind, lookback=3):
    """Did price sweep a level and close back through it within the last
    `lookback` bars? ceiling: a bar wicks above (high > level) but closes below
    (rejection). floor: a bar wicks below (low < level) but closes above
    (reclaim). Returns bool."""
    for k in candles[-lookback:] if lookback > 0 else []:
        hi, lo, cl = float(k["h"]), float(k["l"]), float(k["c"])
        if kind == "ceiling" and hi > level_price and cl < level_price:
            return True
        if kind == "floor" and lo < level_price and cl > level_price:
            return True
    return False


def with_entry_sweeps(structure_out, entry_candles, *, lookback=3):
    """Two-timeframe helper: take a structure read whose LEVELS came from a
    higher timeframe (e.g. 15m) and recompute the sweep_reclaim / sweep_rejection
    flags on a FINER entry timeframe (e.g. 5m) against those same levels. This
    is what lets the scalp enter on a fast 5m sweep of a slow 15m level. Returns
    a shallow copy; never raises."""
    s = dict(structure_out) if isinstance(structure_out, dict) else {}
    ca, fb = s.get("ceiling_above"), s.get("floor_below")
    s["sweep_rejection"] = (detect_sweep(entry_candles, ca["price"], "ceiling", lookback)
                            if ca else False)
    s["sweep_reclaim"] = (detect_sweep(entry_candles, fb["price"], "floor", lookback)
                          if fb else False)
    return s


def _trend(pivots, n):
    """'up' if the last n+1 swing highs are strictly rising AND the last n+1
    swing lows are strictly rising; 'down' for the mirror; else None."""
    highs = [p["price"] for p in pivots if p["kind"] == "high"]
    lows = [p["price"] for p in pivots if p["kind"] == "low"]
    if len(highs) < n + 1 or len(lows) < n + 1:
        return None
    h, l = highs[-(n + 1):], lows[-(n + 1):]
    rising_h = all(h[i] > h[i - 1] for i in range(1, len(h)))
    rising_l = all(l[i] > l[i - 1] for i in range(1, len(l)))
    falling_h = all(h[i] < h[i - 1] for i in range(1, len(h)))
    falling_l = all(l[i] < l[i - 1] for i in range(1, len(l)))
    if rising_h and rising_l:
        return "up"
    if falling_h and falling_l:
        return "down"
    return None


def _classify(tested_ceils, tested_floors, px, pivots, p):
    top = max((l["price"] for l in tested_ceils), default=None)
    bot = min((l["price"] for l in tested_floors), default=None)
    if bot is not None and px < bot:
        return "breakdown"
    if top is not None and px > top:
        return "breakout"
    tr = _trend(pivots, p["trend_swings"])
    if tr == "up":
        return "trend-up"
    if tr == "down":
        return "trend-down"
    if top is not None and bot is not None and bot <= px <= top:
        return "range"
    return "unknown"


def _empty(px, note):
    return {
        "price": px,
        "classification": "unknown",
        "ceilings": [],
        "floors": [],
        "ceiling_above": None,
        "floor_below": None,
        "mid": None,
        "atr": None,
        "sweep_reclaim": False,
        "sweep_rejection": False,
        "_note": note,
    }


def classify_structure(candles, price=None, *, params=None):
    """Compose the primitives into a structure read for one candle series.

    candles: [{o,h,l,c,...}, ...] for a single interval (caller picks 5m/15m).
    price:   current mark; defaults to the last close.
    Returns a JSON-safe dict. Never raises; degrades to an 'unknown' shell when
    there is not enough history.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    if not candles or len(candles) < p["pivot_left"] + p["pivot_right"] + 1:
        px = price if price is not None else (float(candles[-1]["c"]) if candles else None)
        return _empty(px, "insufficient candles for structure")

    px = price if price is not None else float(candles[-1]["c"])
    a = atr(candles, p["atr_n"])
    eps = (a or 0.0) * p["eps_atr_mult"]
    piv = swing_pivots(candles, p["pivot_left"], p["pivot_right"])
    ceilings = cluster_levels(piv, "high", eps)
    floors = cluster_levels(piv, "low", eps)
    tested_ceils = [l for l in ceilings if l["touches"] >= p["min_touches"]]
    tested_floors = [l for l in floors if l["touches"] >= p["min_touches"]]

    ceiling_above = min((l for l in tested_ceils if l["price"] >= px),
                        key=lambda x: x["price"], default=None)
    floor_below = max((l for l in tested_floors if l["price"] <= px),
                      key=lambda x: x["price"], default=None)
    mid = (round((ceiling_above["price"] + floor_below["price"]) / 2, 6)
           if ceiling_above and floor_below else None)

    classification = _classify(tested_ceils, tested_floors, px, piv, p)
    sweep_reclaim = (detect_sweep(candles, floor_below["price"], "floor", p["sweep_lookback"])
                     if floor_below else False)
    sweep_rejection = (detect_sweep(candles, ceiling_above["price"], "ceiling", p["sweep_lookback"])
                       if ceiling_above else False)

    return {
        "price": px,
        "classification": classification,
        "ceilings": tested_ceils,
        "floors": tested_floors,
        "ceiling_above": ceiling_above,
        "floor_below": floor_below,
        "mid": mid,
        "atr": round(a, 6) if a is not None else None,
        "sweep_reclaim": sweep_reclaim,
        "sweep_rejection": sweep_rejection,
        "params": p,
        "_note": ("classification in {range,breakout,breakdown,trend-up,trend-down,unknown}. "
                  "Levels are pivot clusters with touches>=min_touches ('tested'). "
                  "sweep_reclaim/rejection flag a recent wick through the nearest floor/ceiling "
                  "that closed back. READ-ONLY structure; triggers.py turns these into entries."),
    }
