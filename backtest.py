"""Strategy backtest for the deterministic scalp triggers — the go/no-go gate.

Walks historical candles bar by bar, runs the SAME structure.py + triggers.py +
R:R admission that decide.py uses, and simulates every fired-and-passing trigger
with replay.py's fill/stop/target engine. Reports per-trigger/side expectancy.

Why this measures the right thing, and what it deliberately ignores:
  - Historical flow / taker-delta / behavioral state CANNOT be reconstructed
    (those caches only build live). So this backtests the RAW structure+trigger
    expectancy. Every live-only gate (flow Step 1b, behavioral, regime) only
    ever CUTS or sizes DOWN trades — so raw expectancy is a CONSERVATIVE LOWER
    BOUND on the gated live system, never an optimistic one.
  - Entries are modelled as a market fill at the signal-bar close (a synthetic
    at-entry candle is prepended so replay's PENDING->FILLED starts immediately).
  - Same-candle stop/target conflicts resolve stop-first (replay's conservative
    default; no finer drill here) — pessimistic, consistent with the above.
  - A cooldown suppresses re-counting the same setup on consecutive bars.

Two robustness lenses (mirroring backtest_thresholds.py):
  - PARAMETER SWEEP: is the expectancy on a plateau, or a single-value spike?
  - NOISE INJECTION (Varma §7): perturb prices with rising noise; a real edge
    degrades SMOOTHLY toward zero. Jagged / strengthening-under-noise = fitted.

This is a COMPONENT backtest: it validates trigger expectancy, NOT live P&L.

CLI:
  python3 backtest.py [--coin HYPE] [--days 60] [--side both|long|short]
                      [--sweep] [--noise]
"""

import argparse
import sys
import time

import decide
import replay
import structure
import triggers
from fetch_market import HL_INFO, _post_json

_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


# ---- data ----

def fetch_raw(coin, interval, start_ms, end_ms):
    rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": start_ms, "endTime": end_ms}}, "hyperliquid")
    return [{"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
             "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
            for k in rows or []]


def fetch_history(coin, interval, days, now_ms):
    """Page candleSnapshot (400-bar cap) back `days` and return deduped,
    time-sorted candles with int-ms timestamps."""
    step = _MS[interval]
    span = step * 400
    cur = now_ms - days * 86_400_000
    seen = {}
    while cur < now_ms:
        chunk = fetch_raw(coin, interval, cur, min(cur + span, now_ms))
        if not chunk:
            cur += span
            continue
        for c in chunk:
            seen[c["t"]] = c
        cur = chunk[-1]["t"] + step
    return [seen[t] for t in sorted(seen)]


# ---- walk-forward (pure; takes candles, no network) ----

def run(side, candles, *, warmup=40, horizon=48, cooldown=4,
        struct_params=None, trig_params=None):
    """Simulate `side` triggers across `candles`. Returns a list of trade dicts.
    No look-ahead: structure/triggers at bar i see only candles[:i+1], and
    swing_pivots excludes the unconfirmed tail; the fill sim sees only
    candles[i+1:]."""
    trades = []
    last_fire = -10**9
    for i in range(warmup, len(candles) - 1):
        if i - last_fire < cooldown:
            continue
        window = candles[:i + 1]
        px = window[-1]["c"]
        s = structure.classify_structure(window, price=px, params=struct_params)
        trig = triggers.evaluate(side, s, window, params=trig_params)
        _, block = decide._pick_trigger(trig)
        if not block:
            continue
        synth = {"t": window[-1]["t"], "o": px, "h": px, "l": px, "c": px, "v": 0.0}
        fwd = [synth] + candles[i + 1:i + 1 + horizon]
        res = replay.simulate_trigger(block, side, fwd)
        if res["status"] == "unfilled":
            continue
        last_fire = i
        trades.append({
            "i": i, "t": window[-1]["t"], "trigger": block["trigger"],
            "net_r": res["net_r"], "gross_r": res["r"],
            "status": res["status"], "ambiguous": res["ambiguous"],
        })
    return trades


DAY_MS = 86_400_000


def run_intraday(side, c5, c15, *, warmup5=60, cooldown=6, horizon_cap=288,
                 struct_params=None, trig_params=None, allow=("A", "B")):
    """Faithful scalp walk-forward: LEVELS from 15m structure, SWEEP + ENTRY on
    5m closes, and a same-UTC-day time-stop (never carry past 00:00 UTC). Shares
    structure.with_entry_sweeps with the live engine so backtest == live. Pure;
    takes candles, no network. No look-ahead: 15m levels use only bars fully
    closed before the 5m entry bar's close; the fill sim sees only later 5m bars.
    """
    trades = []
    last_fire = -10**9
    allow = set(allow)
    for i in range(warmup5, len(c5) - 1):
        if i - last_fire < cooldown:
            continue
        t5_close = c5[i]["t"] + _MS["5m"]
        w15 = [c for c in c15 if c["t"] + _MS["15m"] <= t5_close][-200:]
        if len(w15) < 30:
            continue
        px = c5[i]["c"]
        s15 = structure.classify_structure(w15, price=px, params=struct_params)
        s = structure.with_entry_sweeps(s15, c5[max(0, i - 3):i + 1])
        s["price"] = px
        trig = triggers.evaluate(side, s, c5[max(0, i - 6):i + 1], params=trig_params)
        trig = {k: v for k, v in trig.items() if k in allow}
        _, block = decide._pick_trigger(trig)
        if not block:
            continue
        day_end = (c5[i]["t"] // DAY_MS + 1) * DAY_MS
        synth = {"t": c5[i]["t"], "o": px, "h": px, "l": px, "c": px, "v": 0.0}
        fwd = [synth] + [c for c in c5[i + 1:] if c["t"] < day_end][:horizon_cap]
        res = replay.simulate_trigger(block, side, fwd)
        if res["status"] == "unfilled":
            continue
        last_fire = i
        trades.append({
            "i": i, "t": c5[i]["t"], "trigger": block["trigger"],
            "net_r": res["net_r"], "gross_r": res["r"],
            "status": res["status"], "ambiguous": res["ambiguous"],
        })
    return trades


def summarize(trades):
    by = {}
    for t in trades:
        by.setdefault(t["trigger"], []).append(t)
    rows = []
    for name, ts in sorted(by.items()):
        nets = [x["net_r"] for x in ts if x["net_r"] is not None]
        n = len(nets)
        if not n:
            continue
        wins = sum(1 for r in nets if r > 0)
        rows.append({
            "trigger": name, "n": n,
            "win_rate": round(wins / n * 100, 1),
            "expectancy_r": round(sum(nets) / n, 3),
            "total_r": round(sum(nets), 2),
            "ambiguous": sum(1 for x in ts if x["ambiguous"]),
        })
    return rows


# ---- rigor lenses ----

def sweep(side, candles, values, **kw):
    """Expectancy vs stop_buffer_atr — a plateau (not a spike) means the edge
    is not fitted to one buffer value."""
    out = []
    for v in values:
        tp = dict(kw.pop("trig_params", None) or {}, stop_buffer_atr=v)
        rows = summarize(run(side, candles, trig_params=tp,
                             **{k: kw[k] for k in kw if k != "trig_params"}))
        agg = _pool(rows)
        out.append({"stop_buffer_atr": v, **agg})
    return out


def _lcg(seed):
    """Deterministic LCG in [0,1) — avoids Math.random/np and stays reproducible
    without a workflow-forbidden global RNG."""
    x = seed & 0x7FFFFFFF
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield x / 0x7FFFFFFF


def noise(side, candles, levels=(0.0, 0.0005, 0.001, 0.002), reps=3, seed=1, **kw):
    """Perturb h/l/c by rising multiplicative noise; a real edge degrades
    smoothly toward zero. Reports mean expectancy per level across reps."""
    out = []
    rng = _lcg(seed)
    for eps in levels:
        exps = []
        for _ in range(reps):
            if eps == 0.0:
                pert = candles
            else:
                pert = []
                for c in candles:
                    f = 1.0 + eps * (2 * next(rng) - 1)
                    pert.append({**c, "h": c["h"] * f, "l": c["l"] * f, "c": c["c"] * f})
            exps.append(_pool(summarize(run(side, pert, **kw))).get("expectancy_r", 0.0))
        mean = sum(exps) / len(exps)
        out.append({"noise": eps, "expectancy_r": round(mean, 3),
                    "std": round((sum((e - mean) ** 2 for e in exps) / len(exps)) ** 0.5, 3)})
    return out


def _pool(rows):
    n = sum(r["n"] for r in rows)
    if not n:
        return {"n": 0, "expectancy_r": 0.0, "total_r": 0.0}
    total = sum(r["total_r"] for r in rows)
    return {"n": n, "expectancy_r": round(total / n, 3), "total_r": round(total, 2)}


# ---- CLI ----

def _print_rows(title, rows):
    print(f"\n{title}")
    if not rows:
        print("  (no trades)")
        return
    for r in rows:
        print("  " + " | ".join(f"{k} {v}" for k, v in r.items()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="HYPE")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--side", default="both", choices=["both", "long", "short"])
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--noise", action="store_true")
    a = ap.parse_args(argv)

    now_ms = int(time.time() * 1000)
    print(f"Fetching {a.days}d of {a.coin} {a.interval} candles...")
    candles = fetch_history(a.coin, a.interval, a.days, now_ms)
    print(f"  {len(candles)} candles "
          f"({candles[0]['t'] if candles else '-'} .. {candles[-1]['t'] if candles else '-'})")
    if len(candles) < 60:
        print("Not enough history to backtest.")
        return 1

    sides = ["long", "short"] if a.side == "both" else [a.side]
    for side in sides:
        trades = run(side, candles)
        rows = summarize(trades)
        _print_rows(f"=== {a.coin} {side.upper()} — per-trigger expectancy (NET R) ===", rows)
        _print_rows("  POOLED", [_pool(rows)])
        if a.sweep:
            _print_rows(f"  stop_buffer_atr SWEEP ({side})",
                        sweep(side, candles, [0.15, 0.25, 0.35, 0.5]))
        if a.noise:
            _print_rows(f"  NOISE INJECTION ({side})", noise(side, candles))
    print("\nNOTE: raw trigger expectancy = conservative lower bound on the "
          "gated live system (flow/behavioral gates only ever cut trades).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
