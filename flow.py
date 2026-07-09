"""Deterministic volume / order-flow classifier for the scalp skill.

Pure functions over already-fetched data (candles with per-bar volume +
bucketed taker_delta). No I/O, no network — fetch_market.py calls classify()
and attaches the result as out['flow']. Mirrors regime.py: the data layer
computes, the LLM reads (Step 1b of scalp-core.md maps the flags to a
conviction adjustment).

Encodes Samir Varma's "volume is the key signal" heuristics
([[samir-varma-react-to-risk-quant-trading]] §8a) as testable flags:

  - coverage_ok        — can we even see the flow? (<50% = scalping half-blind)
  - aggressor_bias     — are buyers or sellers leaning on the tape?
  - volume_climax      — a volume spike = capitulation (down) / blow-off (up)
  - delta_divergence   — price up on selling / price down on buying = exhaustion
  - breakout_vol_ok    — is the latest move backed by above-average volume?

The classifier is FACTUAL (it states what the flow is doing). Step 1b decides
what that means for conviction — and it can only ever CUT conviction, never
raise it (volume confirms or it doesn't; it never manufactures an edge).
"""

# Initial CANDIDATE thresholds — confirm/retune via backtest_thresholds.py
# (same status as regime.DEFAULT_PARAMS: not final until a plateau sweep
# passes). Conservative by design.
DEFAULT_PARAMS = {
    "interval": "5m",        # near-term execution tape for climax/divergence
    "min_coverage": 50.0,    # below => flow read is unreliable (Varma: half-blind)
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


def max_coverage_pct(taker_delta, windows=_WINDOWS):
    """Best coverage across windows, or None if no window has flow data."""
    covs = []
    for w in windows:
        b = _bucket(taker_delta, w)
        if b is not None and b.get("coverage_pct") is not None:
            covs.append(float(b["coverage_pct"]))
    return round(max(covs), 1) if covs else None


def aggressor_bias(taker_delta, params, windows=_WINDOWS):
    """('buyers'|'sellers'|'balanced', avg_buy_share) from buy_share across
    windows. (None, None) if no window reports a buy_share."""
    shares = []
    for w in windows:
        b = _bucket(taker_delta, w)
        if b is not None and b.get("buy_share_pct") is not None:
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
    if near is None or near.get("buy_share_pct") is None:
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


def classify(candles, taker_delta, *, params=None):
    """Compose primitives into a flow read. Pure; never raises.

    candles: {interval: [candle,...]} (each candle has o/h/l/c/v).
    taker_delta: bucketed dict (or a 'DATA UNAVAILABLE' string).
    Returns a JSON-serializable dict for out['flow'].
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    td = taker_delta if isinstance(taker_delta, dict) else {}
    iv = p["interval"]
    cs = candles.get(iv, []) if isinstance(candles, dict) else []

    cov = max_coverage_pct(td)
    bias, avg_share = aggressor_bias(td, p)
    climax = volume_climax(cs, p)
    diverg = delta_divergence(cs, td, p)
    brk = breakout_vol_ok(cs, p)

    return {
        "interval": iv,
        "max_coverage_pct": cov,
        "coverage_ok": (cov is not None and cov >= p["min_coverage"]),
        "aggressor_bias": bias,
        "avg_buy_share_pct": avg_share,
        "volume_climax": climax,
        "delta_divergence": diverg,
        "breakout_vol_ok": brk,
        "params": p,
        "_note": (
            "Volume/flow read (Varma §8a). coverage_ok False => flow unreliable, "
            "Step 1b caps conviction to low. aggressor_bias must agree with the "
            "trade side or conviction is cut. delta_divergence against the side = "
            "exhaustion, do not chase. volume_climax: 'down'=capitulation "
            "(supports longs/covers), 'up'=blow-off (supports shorts/take-profit) "
            "— a climax in YOUR entry direction means you're late (cut). "
            "breakout_vol_ok False => breakout unconfirmed by volume (cut). "
            "Step 1b only ever CUTS conviction, never raises it."
        ),
    }
