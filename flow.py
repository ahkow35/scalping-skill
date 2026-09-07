"""Deterministic volume / order-flow classifier for the scalp skill.

Pure functions over already-fetched data (candles with per-bar volume +
bucketed taker_delta). No I/O, no network — fetch_market.py calls classify()
and attaches the result as out['flow']. Mirrors regime.py: the data layer
computes, the LLM reads (Step 1b of scalp-core.md maps the flags to a
conviction adjustment).

Encodes Samir Varma's "volume is the key signal" heuristics
([[samir-varma-react-to-risk-quant-trading]] §8a) as testable flags:

  - coverage_ok        — reliable, fresh capture for the execution window
  - aggressor_bias     — are buyers or sellers leaning on the tape?
  - volume_climax      — a volume spike = capitulation (down) / blow-off (up)
  - delta_divergence   — price up on selling / price down on buying = exhaustion
  - breakout_vol_ok    — is the latest move backed by above-average volume?

The classifier is FACTUAL (it states what the flow is doing). Step 1b decides
what that means for conviction — and it can only ever CUT conviction, never
raise it (volume confirms or it doesn't; it never manufactures an edge).
"""

import math

# Initial CANDIDATE thresholds — confirm/retune via backtest_thresholds.py
# (same status as regime.DEFAULT_PARAMS: not final until a plateau sweep
# passes). Conservative by design.
DEFAULT_PARAMS = {
    "interval": "5m",        # near-term execution tape for climax/divergence
    "min_coverage": 50.0,    # only meaningful with explicit capture reliability
    "max_sample_age_ms": 60_000,
    "buyers_min": 58.0,      # avg buy_share >= => buyers leaning
    "sellers_max": 42.0,     # avg buy_share <= => sellers leaning
    "climax_baseline_n": 20, # max bars of volume baseline for a spike
    "climax_mult": 8.0,      # last_vol / median_prior >= => climax bar
    "breakout_baseline_n": 20,
    "breakout_mult": 1.5,    # last_vol / mean_prior >= => move is volume-backed
    "price_lookback": 6,     # bars for the near-term price direction
    "min_bars": 12,          # floor: fewer 5m bars than this => climax/breakout
                             # not computable (fetch_market returns ~19 5m bars,
                             # so the baseline adapts down to what's available)
}

_WINDOWS = ("5m", "15m", "1h")


def _bucket(taker_delta, w):
    b = taker_delta.get(w) if isinstance(taker_delta, dict) else None
    return b if isinstance(b, dict) else None


def _reliable(bucket, params):
    """Legacy percentages and sparse REST snapshots cannot confirm flow."""
    if not bucket or bucket.get("reliable") is not True or bucket.get("capture_complete") is not True:
        return False
    if bucket.get("source") == "recent_trades_rest":
        return False
    try:
        share = bucket.get("buy_share_pct")
        count = bucket.get("trade_count", 0)
        return (not isinstance(share, bool) and 0 <= float(share) <= 100
                and not isinstance(count, bool) and math.isfinite(float(count))
                and float(count) > 0
                and 0 <= float(bucket["sample_age_ms"]) <= params["max_sample_age_ms"]
                and params["min_coverage"] <= float(bucket["coverage_pct"]) <= 100)
    except (KeyError, TypeError, ValueError):
        return False


def closed_candles(candles_iv, *, now_ms=None):
    """Use explicit close state or exchange close timestamps when available.

    Older snapshots lack those fields: conservatively omit their final bar.
    This fallback also excludes a lone bar whose close cannot be established.
    """
    closed = []
    for i, candle in enumerate(candles_iv or []):
        end = candle.get("T")
        if now_ms is not None and end is not None:
            try:
                if float(end) < now_ms:
                    closed.append(candle)
            except (TypeError, ValueError):
                pass
        elif "closed" in candle:
            if candle["closed"] is True:
                closed.append(candle)
        elif i < len(candles_iv) - 1:
            closed.append(candle)
    return closed


def max_coverage_pct(taker_delta, windows=_WINDOWS):
    """Best coverage across windows, or None if no window has flow data."""
    covs = []
    for w in windows:
        b = _bucket(taker_delta, w)
        if b is not None and b.get("coverage_pct") is not None:
            try:
                value = float(b["coverage_pct"])
                if not isinstance(b["coverage_pct"], bool) and 0 <= value <= 100:
                    covs.append(value)
            except (TypeError, ValueError):
                pass
    return round(max(covs), 1) if covs else None


def aggressor_bias(taker_delta, params, windows=_WINDOWS):
    """Average only explicitly reliable windows; unavailable otherwise."""
    shares = []
    for w in windows:
        b = _bucket(taker_delta, w)
        if _reliable(b, params) and b.get("buy_share_pct") is not None:
            shares.append(float(b["buy_share_pct"]))
    if not shares:
        return None, None
    avg = sum(shares) / len(shares)
    if avg >= params["buyers_min"]:
        bias = "buyers"
    elif avg <= params["sellers_max"]:
        bias = "sellers"
    else:
        bias = "balanced"
    return bias, round(avg, 1)


def _vols(candles_iv):
    return [float(k["v"]) for k in candles_iv if k.get("v") is not None]


def volume_climax(candles_iv, params):
    """Spike on the last bar vs a robust (median) baseline. Returns
    {ratio, direction} where direction is the climax bar's own move
    ('down' = capitulation, 'up' = blow-off), or None."""
    vols = _vols(candles_iv)
    if len(vols) < params["min_bars"] + 1:
        return None
    n = min(params["climax_baseline_n"], len(vols) - 1)
    last = vols[-1]
    prior = sorted(vols[-(n + 1):-1])
    med = prior[len(prior) // 2]
    if med <= 0:
        return None
    ratio = last / med
    if ratio < params["climax_mult"]:
        return None
    k = candles_iv[-1]
    direction = "up" if float(k["c"]) >= float(k["o"]) else "down"
    return {"ratio": round(ratio, 1), "direction": direction}


def price_direction(candles_iv, lookback):
    """'up'|'down'|'flat'|None over the last `lookback` closes."""
    if len(candles_iv) < 2:
        return None
    lookback = min(lookback, len(candles_iv) - 1)
    closes = [float(k["c"]) for k in candles_iv]
    base = closes[-1 - lookback]
    if base == 0:
        return None
    chg = (closes[-1] - base) / base
    return "up" if chg > 0 else ("down" if chg < 0 else "flat")


def delta_divergence(candles_iv, taker_delta, params):
    """'bearish' (price up while sellers aggressive) / 'bullish' (price down
    while buyers aggressive) / None. The near-term exhaustion tell."""
    pd = price_direction(candles_iv, params["price_lookback"])
    if pd not in ("up", "down"):
        return None
    near = _bucket(taker_delta, params["interval"])
    if not _reliable(near, params) or near.get("buy_share_pct") is None:
        return None
    bs = float(near["buy_share_pct"])
    if pd == "up" and bs <= params["sellers_max"]:
        return "bearish"
    if pd == "down" and bs >= params["buyers_min"]:
        return "bullish"
    return None


def breakout_vol_ok(candles_iv, params):
    """Is the last bar's volume >= breakout_mult × mean of the prior baseline?
    None if not enough bars. The 'is this breakout real?' check."""
    vols = _vols(candles_iv)
    if len(vols) < params["min_bars"] + 1:
        return None
    n = min(params["breakout_baseline_n"], len(vols) - 1)
    prior = vols[-(n + 1):-1]
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return None
    return vols[-1] >= params["breakout_mult"] * avg


def classify(candles, taker_delta, *, params=None, now_ms=None):
    """Compose primitives into a flow read. Pure; never raises.

    candles: {interval: [candle,...]} (each candle has o/h/l/c/v).
    taker_delta: bucketed dict (or a 'DATA UNAVAILABLE' string).
    Returns a JSON-serializable dict for out['flow'].
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    td = taker_delta if isinstance(taker_delta, dict) else {}
    iv = p["interval"]
    cs = closed_candles(candles.get(iv, []), now_ms=now_ms) if isinstance(candles, dict) else []

    cov = max_coverage_pct(td)
    bias, avg_share = aggressor_bias(td, p)
    climax = volume_climax(cs, p)
    diverg = delta_divergence(cs, td, p)
    brk = breakout_vol_ok(cs, p)

    return {
        "interval": iv,
        "max_coverage_pct": cov,
        "coverage_ok": _reliable(_bucket(td, iv), p),
        "capture_reliable": _reliable(_bucket(td, iv), p),
        "reliable_windows": [w for w in _WINDOWS if _reliable(_bucket(td, w), p)],
        "closed_bar_count": len(cs),
        "aggressor_bias": bias,
        "avg_buy_share_pct": avg_share,
        "volume_climax": climax,
        "delta_divergence": diverg,
        "breakout_vol_ok": brk,
        "params": p,
        "_note": (
            "Volume/flow read (Varma §8a). coverage_ok requires explicit reliable "
            "capture and fresh trades in the execution window; otherwise WAIT. "
            "REST sample span never proves coverage. aggressor_bias must agree with the "
            "trade side or conviction is cut. delta_divergence against the side = "
            "exhaustion, do not chase. volume_climax: 'down'=capitulation "
            "(supports longs/covers), 'up'=blow-off (supports shorts/take-profit) "
            "— a climax in YOUR entry direction means you're late (cut). "
            "breakout_vol_ok False => breakout unconfirmed by volume (cut). "
            "Step 1b only ever CUTS conviction, never raises it."
        ),
    }
