"""scalp2 universe selection: HYPE + top-realized-vol liquid HL perps.

Deterministic, injectable fetchers for tests. BTC is macro input only and
never a tradable (spec §4).
"""
import math
import time

from fetch_market import fetch_core_meta, fetch_candles

MIN_OI_USDC = 50_000_000
MIN_DAY_VOL_USDC = 25_000_000
ROTATION_SLOTS = 3
VOL_RANK_POOL = 12
_H24_MS = 24 * 3600_000


def realized_vol_24h(candles_1h):
    closes = [float(c["c"]) for c in candles_1h]
    return sum(abs(math.log(b / a)) for a, b in zip(closes, closes[1:]))


def select_universe(now_ms=None, *, meta=None, candle_fetch=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    meta = meta or fetch_core_meta()
    candle_fetch = candle_fetch or fetch_candles
    names = [u["name"] for u in meta[0]["universe"]]
    eligible = []
    for name, ctx in zip(names, meta[1]):
        if name in ("BTC", "HYPE"):
            continue
        mark = float(ctx["markPx"])
        oi_usdc = float(ctx["openInterest"]) * mark
        day_vol = float(ctx["dayNtlVlm"])
        if oi_usdc >= MIN_OI_USDC and day_vol >= MIN_DAY_VOL_USDC:
            eligible.append((day_vol, name))
    pool = [n for _, n in sorted(eligible, reverse=True)[:VOL_RANK_POOL]]
    ranked = sorted(
        pool,
        key=lambda c: realized_vol_24h(
            candle_fetch(c, "1h", now_ms - _H24_MS, now_ms)),
        reverse=True)
    return ["HYPE"] + ranked[:ROTATION_SLOTS]
