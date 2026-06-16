"""Deterministic market-regime classifier for the scalp skill.

Pure functions over already-fetched data (candles, taker_delta, book). No I/O,
no network — fetch_market.py calls classify() and attaches the result. Mirrors
the taker_delta / microprice pattern: data layer computes, the LLM reads.

Phase 1 ships the READ (regime label + coarse fade_ok). The trade-grade
edge-proximity fade_ok guard and the passive-fade module are Plan 2.
"""


def true_ranges(candles):
    """Wilder true range per bar. First bar uses high-low (no prior close)."""
    trs = []
    prev_close = None
    for k in candles:
        hi, lo = float(k["h"]), float(k["l"])
        if prev_close is None:
            trs.append(round(hi - lo, 10))
        else:
            trs.append(round(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)), 10))
        prev_close = float(k["c"])
    return trs


def range_compression(candles, recent_n=6, baseline_n=20):
    """recent avg TR / baseline avg TR. <1 compressing (quiet), >1 expanding.

    None if fewer than baseline_n bars (not enough history to judge)."""
    if len(candles) < baseline_n or recent_n > baseline_n:
        return None
    trs = true_ranges(candles)
    recent = trs[-recent_n:]
    baseline = trs[-baseline_n:]
    base_avg = sum(baseline) / len(baseline)
    if base_avg <= 0:
        return None
    return round((sum(recent) / len(recent)) / base_avg, 3)


def directionality(candles):
    """|net close move| / sum|bar-to-bar close move| over the series, in [0,1].

    ~1 = clean trend (all moves same direction); ~0 = chop (path cancels out).
    None if fewer than 2 closes or a degenerate flat path."""
    if len(candles) < 2:
        return None
    closes = [float(k["c"]) for k in candles]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path <= 0:
        return None
    return round(net / path, 3)


def _returns(candles):
    closes = [float(k["c"]) for k in candles]
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] != 0]


def btc_corr(primary_candles, btc_candles, lookback=32):
    """Pearson correlation of primary vs BTC returns over the last `lookback`
    aligned bars. None if either series is too short. Assumes both candle lists
    are the same interval and end at the same time (caller aligns by fetching
    matching windows)."""
    n = min(len(primary_candles), len(btc_candles))
    if n < lookback:
        return None
    p = _returns(primary_candles[-lookback:])
    b = _returns(btc_candles[-lookback:])
    m = min(len(p), len(b))
    if m < 3:
        return None
    p, b = p[-m:], b[-m:]
    mp, mb = sum(p) / m, sum(b) / m
    cov = sum((p[i] - mp) * (b[i] - mb) for i in range(m))
    vp = sum((p[i] - mp) ** 2 for i in range(m))
    vb = sum((b[i] - mb) ** 2 for i in range(m))
    if vp <= 0 or vb <= 0:
        return None
    return round(cov / (vp ** 0.5 * vb ** 0.5), 3)


def two_sided_flow(taker_delta, windows=("5m", "15m", "1h")):
    """Score in [0,1]: 1 = aggressor flow perfectly balanced near 50/50
    (mean-reverting tape), 0 = fully one-sided (trending). None if no window
    has volume (all buy_share_pct None)."""
    devs = []
    for w in windows:
        bucket = taker_delta.get(w) if isinstance(taker_delta, dict) else None
        if not isinstance(bucket, dict):
            continue
        bs = bucket.get("buy_share_pct")
        if bs is None:
            continue
        devs.append(abs(float(bs) - 50.0) / 50.0)
    if not devs:
        return None
    return round(1.0 - sum(devs) / len(devs), 3)


# Initial CANDIDATE thresholds — confirmed / retuned by backtest_thresholds.py
# (Task 6). Do not treat as final until the plateau sweep passes.
DEFAULT_PARAMS = {
    "recent_n": 6,
    "baseline_n": 20,
    "corr_lookback": 32,
    "compression_quiet": 0.6,     # below => quiet
    "compression_trend": 1.5,     # above => expanding/trend
    "directionality_trend": 0.5,  # above => trending
    "directionality_chop": 0.3,   # below (with compression) => quiet/chop
    "btc_corr_high": 0.7,         # above (with low directionality) => correlated-chop
    "two_sided_min": 0.6,         # above => mean-reverting tape
    "book_lean_bps": 6.0,         # |microprice_dev_bps| beyond => about-to-break guard
}


def classify(candles, btc_candles, taker_delta, book, *, params=None):
    """Compose primitives into a regime read. Pure; never raises.

    candles/btc_candles: {interval: [candle,...]} dicts (as fetch_market builds).
    taker_delta: the bucketed dict (or a 'DATA UNAVAILABLE' string).
    book: the fetch_l2 dict (uses book['execution']['microprice_dev_bps']).
    Returns a dict safe to JSON-serialize and attach as out['regime'].
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    c15 = candles.get("15m", []) if isinstance(candles, dict) else []
    c1h = candles.get("1h", []) if isinstance(candles, dict) else []
    b15 = btc_candles.get("15m", []) if isinstance(btc_candles, dict) else []
    td = taker_delta if isinstance(taker_delta, dict) else {}

    comp = range_compression(c15, p["recent_n"], p["baseline_n"])
    direction = directionality(c1h)
    corr = btc_corr(c15, b15, p["corr_lookback"])
    tsf = two_sided_flow(td)
    dev = None
    if isinstance(book, dict):
        ex = book.get("execution") or {}
        dev = ex.get("microprice_dev_bps")

    label = _label(comp, direction, corr, tsf, p)
    book_safe = dev is not None and abs(float(dev)) <= p["book_lean_bps"]
    fade_ok = (label == "ranging") and book_safe

    return {
        "range_compression": comp,
        "directionality": direction,
        "btc_corr": corr,
        "two_sided_flow": tsf,
        "microprice_dev_bps": dev,
        "regime_label": label,
        "fade_ok": fade_ok,
        "params": p,
        "_note": ("regime_label in {trending,ranging,quiet,correlated-chop,unknown}. "
                  "fade_ok is COARSE in Phase 1 (regime + book-lean only); the "
                  "trade-grade edge-proximity fade_ok guard and the passive-fade module are Plan 2. "
                  "Passive fades only when regime_label=ranging AND fade_ok=true; "
                  "quiet/correlated-chop => sit out; trending => directional only."),
    }


def _label(comp, direction, corr, tsf, p):
    if comp is None or direction is None:
        return "unknown"
    if comp < p["compression_quiet"] and direction < p["directionality_chop"]:
        return "quiet"
    if corr is not None and corr > p["btc_corr_high"] and direction < (p["directionality_trend"] - 0.1):
        return "correlated-chop"
    if direction > p["directionality_trend"] or comp > p["compression_trend"]:
        return "trending"
    if tsf is not None and tsf >= p["two_sided_min"]:
        return "ranging"
    return "quiet"
