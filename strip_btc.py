"""Deterministic strip-BTC (idiosyncrasy) classifier for the scalp skill.

Pure functions over already-fetched data (primary + BTC candles, plus the
BTC-correlation already computed by regime.py). No I/O, no network —
fetch_market.py calls classify() and attaches the result as out['strip_btc'].
Mirrors regime.py / flow.py: the data layer computes, the LLM reads (Step 1c of
scalp-core.md applies the conviction cut).

The idea (partial-correlation "filter the confounder" logic from the ML
pairs-trading deep-read, Rotondi & Russo 2025): a directional scalp should be
paid for a COIN-SPECIFIC move, not for BTC beta the whole market is riding.
Strip BTC and ask — is this move idiosyncratic, or is the index dragging the
coin along?

This is FACTUAL + a decision: it classifies the recent move as beta-driven or
idiosyncratic and returns cut=True only for the unambiguous pure-beta case. Like
the flow gate it can only ever CUT conviction (one tier), never raise it. It is
direction-neutral: beta authorship of the move does not depend on whether the
trade is long or short (either way you'd be trading a BTC view, not a coin edge).

Distinct from the macro veto: the veto hard-stops on BTC *danger*; this cuts on
BTC *authorship* of the move even when macro is CLEAR.

Blind spot (documented, not fixed here): btc_corr is Pearson (linear), so a coin
nonlinearly driven by BTC (calm when quiet, dumps hard when BTC dumps) can show
low linear corr and slip this gate — the macro veto is the backstop.
"""

import regime as regime_mod

# CANDIDATE thresholds — provisional, same status as regime.DEFAULT_PARAMS.
# corr_high mirrors regime's btc_corr_high (0.7) so the two reads agree on what
# "strongly BTC-correlated" means. Revisit after ~20 resolved trades via
# /scalp summary + replay (first gate where btc_corr affects conviction).
DEFAULT_PARAMS = {
    "corr_lookback": 32,       # 15m bars, matches regime.btc_corr window
    "corr_high": 0.7,          # >= => strongly BTC-correlated (matches regime)
    "corr_low": 0.4,           # <  => decoupled, never a beta cut
    "move_interval": "1h",     # horizon for the recent directional move
    "move_lookback": 6,        # bars (6x1h = ~6h)
    "outrun_mult": 1.5,        # coin |move| > mult x BTC |move| => idiosyncratic
    "min_btc_move_pct": 0.15,  # |BTC move| below this = BTC ~flat: coin move
                               # cannot be "beta" of a non-move => no cut
}


def recent_move_pct(candles_iv, lookback):
    """Signed % change over the last `lookback` closes. None if too short."""
    if not isinstance(candles_iv, list) or len(candles_iv) < 2:
        return None
    lookback = min(lookback, len(candles_iv) - 1)
    closes = [float(k["c"]) for k in candles_iv]
    base = closes[-1 - lookback]
    if base == 0:
        return None
    return round((closes[-1] - base) / base * 100.0, 3)


def classify(candles, btc_candles, *, corr=None, params=None):
    """Compose the strip-BTC read. Pure; never raises.

    candles / btc_candles: {interval: [candle,...]} dicts (as fetch_market builds).
    corr: the BTC correlation already computed by regime (out['regime']['btc_corr']);
          if None, computed here from the 15m windows so the helper is standalone.
    Returns a JSON-serializable dict for out['strip_btc'].

    status in {unavailable, decoupled, moderate, idiosyncratic, beta}.
    cut=True (one tier) only for status == 'beta' (unambiguous pure BTC beta).
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    c = candles if isinstance(candles, dict) else {}
    b = btc_candles if isinstance(btc_candles, dict) else {}
    iv = p["move_interval"]

    if corr is None:
        corr = regime_mod.btc_corr(
            c.get("15m", []), b.get("15m", []), p["corr_lookback"])

    coin_move = recent_move_pct(c.get(iv, []), p["move_lookback"])
    btc_move = recent_move_pct(b.get(iv, []), p["move_lookback"])

    status, cut, reason = _decide(corr, coin_move, btc_move, p)

    return {
        "btc_corr": corr,
        "coin_move_pct": coin_move,
        "btc_move_pct": btc_move,
        "move_interval": iv,
        "move_lookback": p["move_lookback"],
        "status": status,
        "cut": cut,
        "cut_tiers": 1 if cut else 0,
        "reason": reason,
        "params": p,
        "_note": (
            "Strip-BTC idiosyncrasy read (Step 1c). status: beta = move is "
            "BTC-driven (high corr, aligned, not outrunning) => CUT one conviction "
            "tier; idiosyncratic/decoupled/moderate/unavailable => no cut. "
            "Direction-neutral; only ever cuts. btc_corr is linear — a "
            "nonlinearly-BTC-driven coin (dumps only when BTC dumps) can slip this; "
            "the macro veto is the backstop."
        ),
    }


def _decide(corr, coin_move, btc_move, p):
    """Return (status, cut, reason). Pure decision table."""
    if corr is None or coin_move is None or btc_move is None:
        return "unavailable", False, "data unavailable — gate skipped, no cut"
    if corr < p["corr_low"]:
        return ("decoupled", False,
                f"decoupled: corr {corr} < {p['corr_low']} — move is not a BTC read, no cut")
    if corr < p["corr_high"]:
        return ("moderate", False,
                f"moderate corr {corr} (< {p['corr_high']}) — not clearly BTC-driven, no cut")
    # corr >= corr_high: strongly BTC-correlated — decide beta vs idiosyncratic.
    if abs(btc_move) < p["min_btc_move_pct"]:
        return ("idiosyncratic", False,
                f"BTC ~flat ({btc_move:+.2f}%) while coin {coin_move:+.2f}% — "
                "move is not beta, no cut")
    aligned = (coin_move > 0) == (btc_move > 0) and coin_move != 0
    outrunning = abs(coin_move) > p["outrun_mult"] * abs(btc_move)
    if aligned and not outrunning:
        return ("beta", True,
                f"BTC-beta: corr {corr}, coin {coin_move:+.2f}% vs BTC {btc_move:+.2f}% "
                "(aligned, not outrunning) — index-driven, cut 1 tier")
    if not aligned:
        return ("idiosyncratic", False,
                f"idiosyncratic: coin {coin_move:+.2f}% vs BTC {btc_move:+.2f}% "
                "(opposing BTC) — coin-specific, no cut")
    return ("idiosyncratic", False,
            f"idiosyncratic: coin {coin_move:+.2f}% outruns BTC {btc_move:+.2f}% "
            f"(> {p['outrun_mult']}x) — coin-specific residual, no cut")
