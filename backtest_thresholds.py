"""Threshold backtest for the scalp skill's MECHANICAL macro-veto conditions.

The skill's veto thresholds were first-pass guesses (flagged as such in the
project doc). This script tests the two conditions that are computable from
history, against what the primary coin actually did next:

  1. BTC 1h range >= MULT x trailing 20-bar average range, directional
     (down vetoes longs, up vetoes shorts).
  2. Primary-coin funding extremes (per-8h |rate| >= threshold; positive
     extreme vetoes longs, negative vetoes shorts).

NOT testable: the BTC.D 24h-change condition — no free historical BTC
dominance series exists (CoinGecko /global is snapshot-only; that's why the
skill maintains its own rolling cache). Stated, not silently skipped.

Method: at every 1h BTC/primary candle close, compute condition flags, then
measure the primary coin's forward return and max adverse excursion over
1h/4h/24h. Compare veto-flagged hours against baseline. A protective veto
shows materially worse long outcomes on "down" flags (and worse short
outcomes on "up" flags). A threshold sweep checks the chosen values sit on a
plateau, not a spike (parameter sensitivity).

This is a COMPONENT backtest: it validates the gate, never strategy P&L.

CLI:
  python3 backtest_thresholds.py [--coin HYPE] [--days 365]
"""

import argparse
import json
import sys
import time

from fetch_market import _post_json, HL_INFO
from replay import fetch_candles_ms

H1_MS = 3_600_000


# ---------------------------------------------------------------- pure core

def atr_veto_flags(candles, mult=2.0, lookback=20):
    """Per-candle flag: 'down'/'up' when range >= mult x trailing avg range
    and the bar is directional; None otherwise (or while warming up)."""
    flags = []
    for i, cd in enumerate(candles):
        if i < lookback:
            flags.append(None)
            continue
        trailing = candles[i - lookback:i]
        avg_range = sum(x["h"] - x["l"] for x in trailing) / lookback
        rng = cd["h"] - cd["l"]
        if avg_range <= 0 or rng < mult * avg_range:
            flags.append(None)
            continue
        flags.append("down" if cd["c"] < cd["o"] else "up")
    return flags


def forward_stats(candles, horizon_bars):
    """Forward return + max adverse excursion per bar, close-to-close.

    mae_long  = (min low over window) / entry close - 1   (<= 0)
    mae_short = -((max high over window) / entry close - 1) (<= 0)
    None where fewer than horizon_bars of future data exist.
    """
    out = []
    for i, cd in enumerate(candles):
        if i + horizon_bars >= len(candles):
            out.append(None)
            continue
        entry = cd["c"]
        window = candles[i + 1:i + 1 + horizon_bars]
        out.append({
            "ret": candles[i + horizon_bars]["c"] / entry - 1,
            "mae_long": min(x["l"] for x in window) / entry - 1,
            "mae_short": -(max(x["h"] for x in window) / entry - 1),
        })
    return out


def align_funding(candles, funding_rows):
    """Per-candle funding as a PER-8H rate (skill thresholds are per-8h).

    HL fundingHistory rows are hourly; map by timestamp, x8. None when no
    row exists for that hour.
    """
    # HL funding timestamps drift a few ms off the hour — bucket to the hour
    by_hour = {round(int(r["time"]) / H1_MS): float(r["fundingRate"])
               for r in funding_rows}
    return [
        (by_hour[h] * 8) if (h := round(int(cd["t"]) / H1_MS)) in by_hour
        else None
        for cd in candles
    ]


def summarize(rows, mae_key):
    """Aggregate forward-stat rows: mean return, disaster rate, mean MAE."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return {"n": 0}
    rets = sorted(r["ret"] for r in rows)
    n = len(rows)
    return {
        "n": n,
        "mean_ret_pct": 100 * sum(rets) / n,
        "median_ret_pct": 100 * rets[n // 2],
        "p10_ret_pct": 100 * rets[max(0, int(n * 0.1) - 1)],
        "p_disaster": sum(1 for r in rets if r < -0.02) / n,
        "mean_mae_pct": 100 * sum(r[mae_key] for r in rows) / n,
    }


# ------------------------------------------------------------------- fetch

def fetch_funding_history(coin, start_ms, end_ms):
    """Chunked HL fundingHistory pull (hourly rows)."""
    out, seen = [], set()
    chunk = 400 * H1_MS
    t = start_ms
    while t < end_ms:
        rows = _post_json(HL_INFO, {
            "type": "fundingHistory", "coin": coin,
            "startTime": t, "endTime": min(t + chunk, end_ms)},
            "hyperliquid")
        for r in rows or []:
            ts = int(r["time"])
            if ts not in seen:
                seen.add(ts)
                out.append(r)
        t += chunk
    return out


# ------------------------------------------------------------------ report

HORIZONS = {"1h": 1, "4h": 4, "24h": 24}
ATR_SWEEP = (1.5, 2.0, 2.5, 3.0)
FUNDING_SWEEP = (0.0003, 0.0005, 0.0008, 0.001)  # per-8h decimals


def run(coin="HYPE", days=365):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000

    btc = fetch_candles_ms("BTC", "1h", H1_MS, start_ms, now_ms)
    primary = (btc if coin == "BTC"
               else fetch_candles_ms(coin, "1h", H1_MS, start_ms, now_ms))
    funding = fetch_funding_history(coin, start_ms, now_ms)

    # align primary candles to BTC hours (intersection on timestamps)
    btc_by_t = {cd["t"]: cd for cd in btc}
    primary = [cd for cd in primary if cd["t"] in btc_by_t]
    btc_aligned = [btc_by_t[cd["t"]] for cd in primary]
    fund_8h = align_funding(primary, funding)

    fwd = {name: forward_stats(primary, bars)
           for name, bars in HORIZONS.items()}

    report = {
        "coin": coin,
        "days": days,
        "bars": len(primary),
        "note_btcd": "BTC.D condition NOT backtestable — no free historical "
                     "dominance series; only live cache going forward.",
        "atr_condition": {},
        "funding_condition": {},
    }

    for mult in ATR_SWEEP:
        flags = atr_veto_flags(btc_aligned, mult=mult)
        block = {}
        for hname in HORIZONS:
            rows = fwd[hname]
            down = [rows[i] for i, f in enumerate(flags) if f == "down"]
            up = [rows[i] for i, f in enumerate(flags) if f == "up"]
            base = [rows[i] for i, f in enumerate(flags) if f is None]
            block[hname] = {
                "veto_down_for_longs": summarize(down, "mae_long"),
                "veto_up_for_shorts": summarize(up, "mae_short"),
                "baseline": summarize(base, "mae_long"),
            }
        report["atr_condition"][f"{mult}x"] = block

    for thr in FUNDING_SWEEP:
        block = {}
        for hname in HORIZONS:
            rows = fwd[hname]
            pos = [rows[i] for i, f in enumerate(fund_8h)
                   if f is not None and f >= thr]
            neg = [rows[i] for i, f in enumerate(fund_8h)
                   if f is not None and f <= -thr]
            base = [rows[i] for i, f in enumerate(fund_8h)
                    if f is not None and abs(f) < thr]
            block[hname] = {
                "pos_extreme_vetoes_longs": summarize(pos, "mae_long"),
                "neg_extreme_vetoes_shorts": summarize(neg, "mae_short"),
                "baseline": summarize(base, "mae_long"),
            }
        report["funding_condition"][f"{thr * 100:.2f}%/8h"] = block

    return report


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--coin", default="HYPE")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args(argv[1:])
    print(json.dumps(run(coin=args.coin, days=args.days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
