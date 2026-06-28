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

Two robustness lenses:
  - PARAMETER SWEEPS (ATR_SWEEP / FUNDING_SWEEP / regime_compression_sweep):
    "is the chosen threshold on a plateau, not a spike?"
  - NOISE INJECTION (--noise, Varma §7): "is the underlying edge even real?"
    Perturb input prices with rising random noise; a real edge degrades
    SMOOTHLY toward zero. Jagged / strengthening-under-noise = fitted to noise.
    Reports per-level mean + std so a spike can be judged against rep variance.

CLI:
  python3 backtest_thresholds.py [--coin HYPE] [--days 365]   # ATR/funding report
  python3 backtest_thresholds.py --regime [--coin HYPE]       # regime gate backtest
  python3 backtest_thresholds.py --noise  [--coin HYPE]       # noise-injection test
"""

import argparse
import json
import random
import sys
import time

from fetch_market import _post_json, HL_INFO, fetch_candles
from replay import fetch_candles_ms
import regime as _regime_mod

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


# --------------------------------------------------------- regime backtest

# Flow (taker_delta) has no multi-day history (the live trade cache spans ~6h),
# so the backtest cannot reconstruct the two-sided-flow signal. It assumes
# NEUTRAL two-sided flow and validates only the STRUCTURAL regime gate
# (range_compression + directionality + btc_corr). The live `ranging` label
# additionally requires real two-sided flow at decision time — that half is not
# backtestable from candles and is validated live via the audit log instead.
_NEUTRAL_FLOW = {"5m": {"buy_share_pct": 50.0},
                 "15m": {"buy_share_pct": 50.0},
                 "1h": {"buy_share_pct": 50.0}}
_BACKTEST_WINDOW = 48  # trailing bars handed to classify (mimics live bounded candle feed)


def regime_forward_stats(primary_candles, btc_candles, horizon_bars=4,
                         params=None):
    """At each bar i (given enough history), label the regime from a TRAILING
    window of candles ending at i, then measure the forward net return over the
    next `horizon_bars`. Group by label. A useful classifier shows: 'trending'
    bars precede LARGE net moves (continuation) and 'ranging' bars precede SMALL
    net moves (mean-reversion). Returns {label: {n, mean_abs_fwd_ret, mean_fwd_ret}}.

    A trailing window (not the full prefix) is used so directionality reflects
    recent structure, mirroring the live bounded candle feed. Flow is assumed
    neutral (see _NEUTRAL_FLOW) -> validates the structural gate only.
    """
    p = {**_regime_mod.DEFAULT_PARAMS, **(params or {})}
    need = max(p["baseline_n"], p["corr_lookback"])
    buckets = {}
    n = len(primary_candles)
    for i in range(need, n - horizon_bars):
        lo = max(0, i + 1 - _BACKTEST_WINDOW)
        seg = primary_candles[lo:i + 1]
        bseg = btc_candles[lo:i + 1]
        out = _regime_mod.classify({"15m": seg, "1h": seg}, {"15m": bseg},
                                   _NEUTRAL_FLOW, {"execution": {}}, params=p)
        label = out["regime_label"]
        c0 = float(primary_candles[i]["c"])
        c1 = float(primary_candles[i + horizon_bars]["c"])
        if c0 <= 0:
            continue
        fwd = (c1 - c0) / c0
        b = buckets.setdefault(label, {"n": 0, "_sum_abs": 0.0, "_sum": 0.0})
        b["n"] += 1
        b["_sum_abs"] += abs(fwd)
        b["_sum"] += fwd
    result = {}
    for label, b in buckets.items():
        result[label] = {
            "n": b["n"],
            "mean_abs_fwd_ret": round(b["_sum_abs"] / b["n"], 6),
            "mean_fwd_ret": round(b["_sum"] / b["n"], 6),
        }
    return result


def regime_compression_sweep(primary_candles, btc_candles, horizon_bars=4,
                             grid=(0.4, 0.5, 0.6, 0.7, 0.8)):
    """Sweep compression_quiet to check the chosen value sits on a plateau, not
    a spike. Returns {value: {ranging_abs, trending_abs, separation}} so a human
    can confirm stable behavior around DEFAULT_PARAMS."""
    rows = {}
    for v in grid:
        stats = regime_forward_stats(primary_candles, btc_candles,
                                     horizon_bars=horizon_bars,
                                     params={"compression_quiet": v})
        rang = stats.get("ranging", {}).get("mean_abs_fwd_ret")
        trend = stats.get("trending", {}).get("mean_abs_fwd_ret")
        sep = (trend - rang) if (rang is not None and trend is not None) else None
        rows[v] = {"ranging_abs": rang, "trending_abs": trend, "separation": sep}
    return rows


# --------------------------------------------------- noise-injection robustness
#
# Varma's anti-overfit test (note §7): perturb the INPUT PRICES with random
# noise of increasing magnitude and watch the edge metric. A REAL edge degrades
# SMOOTHLY toward zero as noise grows. A jagged / non-monotone response — or an
# edge that gets STRONGER under noise — means the signal was fitted to noise,
# not to data. This complements the parameter sweeps above: a sweep asks "is the
# threshold on a plateau?"; this asks "is the underlying edge even real?".

NOISE_SIGMAS = (0.0, 0.0005, 0.001, 0.002, 0.004, 0.008)  # multiplicative, per bar
NOISE_REPS = 8


def add_noise(candles, sigma, rng):
    """Return candles with each O/H/L/C multiplied by (1 + N(0, sigma)),
    independently per field, then high/low re-fixed to bracket the bar.
    sigma == 0 is the identity (single clean pass)."""
    if sigma <= 0:
        return candles
    out = []
    for cd in candles:
        o = cd["o"] * (1.0 + rng.gauss(0.0, sigma))
        h = cd["h"] * (1.0 + rng.gauss(0.0, sigma))
        l = cd["l"] * (1.0 + rng.gauss(0.0, sigma))
        c = cd["c"] * (1.0 + rng.gauss(0.0, sigma))
        out.append({**cd, "o": o, "c": c, "h": max(o, h, l, c), "l": min(o, h, l, c)})
    return out


def atr_protective_edge(primary, btc, mult=2.0, horizon=4):
    """Edge metric for the ATR veto: how much WORSE forward longs do on a
    'down' veto flag vs baseline (positive = the veto is protective).
    None if either bucket is empty."""
    flags = atr_veto_flags(btc, mult=mult)
    fwd = forward_stats(primary, horizon)
    down = [fwd[i]["ret"] for i, f in enumerate(flags)
            if f == "down" and fwd[i] is not None]
    base = [fwd[i]["ret"] for i, f in enumerate(flags)
            if f is None and fwd[i] is not None]
    if not down or not base:
        return None
    return (sum(base) / len(base)) - (sum(down) / len(down))


def regime_separation_edge(primary, btc, horizon=4):
    """Edge metric for the regime gate: trending bars should precede LARGER
    moves than ranging bars. Edge = trending_abs - ranging_abs (positive =
    the classifier separates the two regimes). None if a bucket is missing."""
    stats = regime_forward_stats(primary, btc, horizon_bars=horizon)
    rang = stats.get("ranging", {}).get("mean_abs_fwd_ret")
    trend = stats.get("trending", {}).get("mean_abs_fwd_ret")
    if rang is None or trend is None:
        return None
    return trend - rang


def noise_robustness(primary, btc, metric_fn, sigmas=NOISE_SIGMAS,
                     reps=NOISE_REPS, seed=12345):
    """Run metric_fn(primary, btc) across noise levels. At each sigma>0, average
    over `reps` independent perturbations (deterministic via seed). Returns a
    curve [{sigma, mean, n}]."""
    rng = random.Random(seed)
    curve = []
    for s in sigmas:
        r = 1 if s <= 0 else reps
        vals = []
        for _ in range(r):
            m = metric_fn(add_noise(primary, s, rng), add_noise(btc, s, rng))
            if m is not None:
                vals.append(m)
        mean = (sum(vals) / len(vals)) if vals else None
        if len(vals) > 1:
            std = (sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5
        else:
            std = (0.0 if vals else None)
        curve.append({"sigma": s, "n": len(vals), "mean": mean,
                      "std": round(std, 6) if std is not None else None})
    return curve


def degradation_verdict(curve):
    """Smooth degradation toward zero = PASS; jagged / strengthening = SUSPECT.

    Checks: (1) the magnitude is mostly non-increasing as noise grows,
    (2) the noisiest level is no stronger than the clean level, (3) no
    intermediate level spikes >10% above the clean magnitude (edge getting
    STRONGER under noise is the classic fitted-to-noise tell)."""
    pts = [c for c in curve if c["mean"] is not None]
    if len(pts) < 3:
        return {"verdict": "INSUFFICIENT", "reason": "fewer than 3 scored levels"}
    mags = [abs(c["mean"]) for c in pts]
    base, final = mags[0], mags[-1]
    steps = len(mags) - 1
    non_increasing = sum(1 for i in range(steps) if mags[i + 1] <= mags[i] + 1e-9)
    degrade_ratio = non_increasing / steps
    spike = any(m > base * 1.10 + 1e-12 for m in mags[1:])
    smooth = degrade_ratio >= 0.6 and final <= base + 1e-12 and not spike
    return {
        "verdict": "PASS" if smooth else "SUSPECT",
        "base_abs": round(base, 6),
        "final_abs": round(final, 6),
        "degrade_ratio": round(degrade_ratio, 3),
        "spike_above_baseline": spike,
        "_note": ("PASS = edge degrades smoothly toward 0 under input noise "
                  "(real signal). SUSPECT = jagged/non-monotone or strengthens "
                  "under noise (likely fitted to noise — Varma §7)."),
    }


def run_noise(coin="HYPE", days=365):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    btc = fetch_candles("BTC", "1h", start_ms, now_ms)
    primary = btc if coin == "BTC" else fetch_candles(coin, "1h", start_ms, now_ms)
    btc_by_t = {cd["t"]: cd for cd in btc}
    primary = [cd for cd in primary if cd["t"] in btc_by_t]
    btc_aligned = [btc_by_t[cd["t"]] for cd in primary]

    out = {"coin": coin, "days": days, "bars": len(primary),
           "sigmas": list(NOISE_SIGMAS), "reps": NOISE_REPS, "tests": {}}
    for name, fn in (("atr_protective_edge",
                      lambda p, b: atr_protective_edge(p, b)),
                     ("regime_separation_edge",
                      lambda p, b: regime_separation_edge(p, b))):
        curve = noise_robustness(primary, btc_aligned, fn)
        out["tests"][name] = {"curve": curve,
                              "verdict": degradation_verdict(curve)}
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--coin", default="HYPE")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--regime", action="store_true",
                    help="Run regime threshold backtest instead of ATR/funding report.")
    ap.add_argument("--noise", action="store_true",
                    help="Run noise-injection robustness test (Varma §7).")
    args = ap.parse_args(argv[1:])
    if args.noise:
        print(json.dumps(run_noise(coin=args.coin, days=args.days), indent=2))
        return 0
    if args.regime:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - args.days * 86_400_000
        primary_1h = fetch_candles(args.coin, "1h", start_ms, now_ms)
        btc_1h = fetch_candles("BTC", "1h", start_ms, now_ms)
        fwd_stats = regime_forward_stats(primary_1h, btc_1h)
        sweep = regime_compression_sweep(primary_1h, btc_1h)
        print(json.dumps({
            "coin": args.coin,
            "days": args.days,
            "bars": len(primary_1h),
            "regime_forward_stats": fwd_stats,
            "compression_sweep": sweep,
        }, indent=2))
        return 0
    print(json.dumps(run(coin=args.coin, days=args.days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
