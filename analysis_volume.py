"""A1/A2/A3 — the pre-registered volume-filter backtest
(PLAN-volume-backtest-2026-07-11.md). Read the plan first; the analysis order
and pass criteria there are frozen and this script must not deviate from them.

A1 — RVOL-quartile expectancy diagnostic (primary). Floor 0.0 (max power),
     entry_mode close, per (trigger, side); binned by signal-bar RVOL
     quartile, per coin + pooled. Answers: is expectancy monotone in RVOL?
A2 — Threshold filters (confirmatory). Floor 2.0, close AND retest.
     F1 participation: signal-bar RVOL >= k, k in {1.5, 2.0, 3.0}, all 4
     triggers. F2 climax: SWEEP-BAR RVOL >= k, sweep triggers only
     (long_A/short_A). 4x2x3 (F1) + 2x2x3 (F2) = 24 + 12 = 36 cells — see
     `## Results` in the plan for a note on the plan's own "48 cells" count,
     which does not match its own stated formula (24 + 12 = 36); this script
     runs exactly the formula as written, not a number invented to hit 48.
A3 — Report-only session-split diagnostic on the A1 (floor-0) population.

Uses backtest.run_intraday (committed, unit-tested) — the SAME two-timeframe
walk-forward the live engine shares. Coins: HYPE (dev) + SOL/INJ/NEAR (OOS),
60d each. Candle fetches are throttled (Hyperliquid candleSnapshot 429-safe)
and cached to disk under .candle_cache/ (gitignored) — reruns only fetch the
incremental tail since the last cached bar.

Run:
  python3 analysis_volume.py                # full A1+A2+A3, all 4 coins, 60d
  python3 analysis_volume.py --coin HYPE     # single coin (fast dev loop)
  python3 analysis_volume.py --refresh       # ignore cache, refetch in full
  python3 analysis_volume.py --a1-only       # skip the (slower) A2 sweep
"""

import argparse
import json
import os
import sys
import time

import backtest
from fetch_market import HL_INFO, _post_json

COINS = ["HYPE", "SOL", "INJ", "NEAR"]
OOS_COINS = ["SOL", "INJ", "NEAR"]
DAYS = 60
FLOOR_A1 = 0.0
FLOOR_A2 = 2.0
TRIGGERS = [("long", "A"), ("long", "B"), ("short", "A"), ("short", "B")]
SWEEP_TRIGGERS = [("long", "A"), ("short", "A")]
F1_KS = (1.5, 2.0, 3.0)
F2_KS = (1.5, 2.0, 3.0)
MODES = ("close", "retest")

_MS = {"5m": 300_000, "15m": 900_000}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".candle_cache")


# ---------------------------------------------------- throttled fetch + cache

def _raw(coin, interval, s, e, delay=0.35, retries=6):
    """One candleSnapshot page, retrying with backoff on HTTP 429."""
    for a in range(retries):
        try:
            rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval, "startTime": s, "endTime": e}},
                "hyperliquid")
            time.sleep(delay)
            return [{"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
                     "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
                    for k in rows or []]
        except Exception as ex:  # noqa: BLE001 — throttle/retry on 429 only
            if "429" in str(ex) or "Too Many" in str(ex):
                time.sleep(3 * (a + 1))
                continue
            raise
    return []


def _fetch_range(coin, interval, start_ms, end_ms):
    step = _MS[interval]
    span = step * 400
    cur, seen = start_ms, {}
    while cur < end_ms:
        chunk = _raw(coin, interval, cur, min(cur + span, end_ms))
        if not chunk:
            cur += span
            continue
        for c in chunk:
            seen[c["t"]] = c
        cur = chunk[-1]["t"] + step
    return [seen[t] for t in sorted(seen)]


def _cache_path(coin, interval, days):
    return os.path.join(CACHE_DIR, f"{coin}_{interval}_{days}d.json")


def history(coin, interval, days, now_ms, refresh=False):
    """days of candle history with disk caching. A fresh cache (last bar
    within 2 intervals of now) is reused untouched; a stale cache only
    fetches the incremental tail and merges. Cold or --refresh -> full
    fetch. UTC-ms timestamps throughout (no local-tz math anywhere here)."""
    path = _cache_path(coin, interval, days)
    step = _MS[interval]
    start = now_ms - days * 86_400_000
    cached = None
    if not refresh and os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
    if cached:
        last_t = cached[-1]["t"]
        if now_ms - last_t < step * 2:
            return [c for c in cached if c["t"] >= start]
        fresh = _fetch_range(coin, interval, last_t + step, now_ms)
        merged = {c["t"]: c for c in cached}
        for c in fresh:
            merged[c["t"]] = c
        out = [merged[t] for t in sorted(merged) if merged[t]["t"] >= start]
    else:
        out = _fetch_range(coin, interval, start, now_ms)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f)
    return out


def load_all(coins, days, now_ms, refresh=False):
    data = {}
    for coin in coins:
        c5 = history(coin, "5m", days, now_ms, refresh=refresh)
        c15 = history(coin, "15m", days, now_ms, refresh=refresh)
        data[coin] = (c5, c15)
        print(f"  {coin}: {len(c5)} x 5m, {len(c15)} x 15m", file=sys.stderr)
    return data


# --------------------------------------------------------------------- stats

def _stat(trades):
    nets = [t["net_r"] for t in trades if t["net_r"] is not None]
    n = len(nets)
    if not n:
        return {"n": 0, "win_pct": 0.0, "net_exp": 0.0, "total_r": 0.0}
    wins = sum(1 for r in nets if r > 0)
    return {"n": n, "win_pct": round(wins / n * 100, 1),
            "net_exp": round(sum(nets) / n, 3), "total_r": round(sum(nets), 2)}


def _sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


# ------------------------------------------------------------ A1: quartiles

def _quartile_bins(trades_with_rvol):
    """Split trades (already filtered to rvol_signal is not None) into 4
    near-equal bins by ascending rvol_signal. Q1 = lowest RVOL, Q4 = highest."""
    ordered = sorted(trades_with_rvol, key=lambda t: t["rvol_signal"])
    n = len(ordered)
    edges = [round(n * i / 4) for i in range(5)]
    labels = ["Q1", "Q2", "Q3", "Q4"]
    return [(labels[i], ordered[edges[i]:edges[i + 1]]) for i in range(4)]


def _quartile_row(label, sub):
    row = {"label": label, **_stat(sub)}
    row["rvol_range"] = ([round(sub[0]["rvol_signal"], 2), round(sub[-1]["rvol_signal"], 2)]
                         if sub else None)
    return row


def monotonicity_read(pooled_quartiles):
    """Pass criterion 3's basis, exactly as pre-registered (plan criterion 3):
    'top quartile >= bottom quartile in net R'. A strict Q1..Q4 staircase is
    NOT required — reviewer finding 2026-07-12: the earlier all-pairs version
    was stricter than the plan and could wrongly fail criterion 3 on a rerun.
    The full ordering is still reported for context."""
    exps = [q["net_exp"] for q in pooled_quartiles if q["n"] > 0]
    if len(exps) < 2:
        return "insufficient data"
    top_ge_bottom = exps[-1] >= exps[0]
    staircase = all(exps[i] <= exps[i + 1] for i in range(len(exps) - 1))
    if top_ge_bottom and staircase and exps[0] < exps[-1]:
        return "monotone increasing"
    if top_ge_bottom:
        return "top >= bottom (non-staircase)"
    return "inverse (top < bottom)"


def a1_table_for_trigger(side, letter, data, coins):
    per_coin = {}
    pooled_trades = []
    for coin in coins:
        c5, c15 = data[coin]
        trades = backtest.run_intraday(side, c5, c15, allow=(letter,),
                                       trig_params={"min_net_rr_t1": FLOOR_A1})
        tagged = [dict(t, coin=coin) for t in trades]
        pooled_trades += tagged
        withrvol = [t for t in tagged if t["rvol_signal"] is not None]
        bins = _quartile_bins(withrvol)
        per_coin[coin] = {
            "n_total": len(tagged),
            "n_excluded_no_rvol": len(tagged) - len(withrvol),
            "quartiles": [_quartile_row(lab, sub) for lab, sub in bins],
        }
    withrvol_pool = [t for t in pooled_trades if t["rvol_signal"] is not None]
    pooled_bins = _quartile_bins(withrvol_pool)
    pooled_quartiles = [_quartile_row(lab, sub) for lab, sub in pooled_bins]
    return {
        "per_coin": per_coin,
        "pooled": {
            "n_total": len(pooled_trades),
            "n_excluded_no_rvol": len(pooled_trades) - len(withrvol_pool),
            "quartiles": pooled_quartiles,
        },
        "monotonicity": monotonicity_read(pooled_quartiles),
        "pooled_trades": pooled_trades,
    }


def a1_all(data, coins):
    return {f"{side}_{letter}": a1_table_for_trigger(side, letter, data, coins)
            for side, letter in TRIGGERS}


# --------------------------------------------------------- A2: threshold sweep

def a2_cell(side, letter, mode, family, k, data):
    per_coin = {}
    pooled_trades = []
    for coin in COINS:
        c5, c15 = data[coin]
        vf = {"kind": family, "min_rvol": k}
        trades = backtest.run_intraday(
            side, c5, c15, allow=(letter,),
            trig_params={"min_net_rr_t1": FLOOR_A2, "entry_mode": mode},
            vol_filter=vf)
        per_coin[coin] = _stat(trades)
        if coin in OOS_COINS:
            pooled_trades += trades
    return {"hype": per_coin["HYPE"], "pooled_oos": _stat(pooled_trades),
            "per_coin": per_coin}


def a2_all(data, a1_results):
    groups = {}
    for side, letter in TRIGGERS:
        for mode in MODES:
            for k in F1_KS:
                key = (side, letter, mode, "F1")
                groups.setdefault(key, {})[k] = a2_cell(side, letter, mode, "F1", k, data)
    for side, letter in SWEEP_TRIGGERS:
        for mode in MODES:
            for k in F2_KS:
                key = (side, letter, mode, "F2")
                groups.setdefault(key, {})[k] = a2_cell(side, letter, mode, "F2", k, data)

    cells = []
    for (side, letter, mode, family), by_k in groups.items():
        trigger_name = f"{side}_{letter}"
        mono = a1_results[trigger_name]["monotonicity"]
        crit3 = mono in ("monotone increasing", "top >= bottom (non-staircase)")
        ks = sorted(by_k)
        for idx, k in enumerate(ks):
            cell = by_k[k]
            crit1 = cell["hype"]["n"] > 0 and cell["hype"]["net_exp"] > 0
            crit2 = cell["pooled_oos"]["n"] >= 30 and cell["pooled_oos"]["net_exp"] >= 0
            neighbor_ks = [ks[j] for j in (idx - 1, idx + 1) if 0 <= j < len(ks)]
            this_sign = _sign(cell["hype"]["net_exp"])
            neighbor_signs = [_sign(by_k[nk]["hype"]["net_exp"]) for nk in neighbor_ks]
            # "isolated spike" (criterion 4 fail): this cell's sign agrees with
            # NEITHER available neighbor.
            crit4 = not (neighbor_signs and all(s != this_sign for s in neighbor_signs))
            checks = {"1_hype_net_exp_gt_0": crit1, "2_pooled_oos_ge_0_n_ge_30": crit2,
                      "3_a1_monotone_consistent": crit3, "4_no_isolated_spike": crit4}
            failed = [name for name, ok in checks.items() if not ok]
            cells.append({
                "trigger": trigger_name, "mode": mode, "family": family, "k": k,
                "hype": cell["hype"], "pooled_oos": cell["pooled_oos"],
                "checks": checks, "pass": all(checks.values()), "failed": failed,
            })
    return cells


# ------------------------------------------------------- A3: session split

def _session(ts_ms):
    """Non-overlapping 8h UTC buckets — a coarse, documented approximation of
    Asia/EU/US, not real session-open times. Report-only diagnostic."""
    hour = int((ts_ms // 3_600_000) % 24)
    if hour < 8:
        return "Asia (00-08 UTC)"
    if hour < 16:
        return "EU (08-16 UTC)"
    return "US (16-24 UTC)"


def a3_session_split(a1_results):
    out = {}
    for trigger_name, res in a1_results.items():
        by_session = {}
        for t in res["pooled_trades"]:
            if t["rvol_signal"] is None:
                continue
            by_session.setdefault(_session(t["t"]), []).append(t)
        out[trigger_name] = {
            sess: {**_stat(trades),
                  "mean_rvol": round(sum(x["rvol_signal"] for x in trades) / len(trades), 2)}
            for sess, trades in by_session.items()
        }
    return out


# -------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--coin", action="append", help="restrict to this coin "
                    "(repeatable); default HYPE+SOL+INJ+NEAR")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--refresh", action="store_true", help="ignore disk cache")
    ap.add_argument("--a1-only", action="store_true", help="skip A2 (faster dev loop)")
    ap.add_argument("--json", action="store_true", help="dump raw JSON instead of tables")
    a = ap.parse_args(argv)

    coins = a.coin or COINS
    now_ms = int(time.time() * 1000)
    print(f"Fetching/loading {a.days}d candles for {coins}...", file=sys.stderr)
    data = load_all(coins, a.days, now_ms, refresh=a.refresh)

    print("Running A1 (RVOL-quartile diagnostic, floor 0.0)...", file=sys.stderr)
    a1 = a1_all(data, coins)

    a2 = None
    if not a.a1_only and set(coins) == set(COINS):
        print("Running A2 (threshold filters, floor 2.0, close+retest)...", file=sys.stderr)
        a2 = a2_all(data, a1)
    elif not a.a1_only:
        print("A2 skipped: needs all 4 coins (HYPE+SOL+INJ+NEAR) for the OOS "
              "pass criteria; got only " + ",".join(coins), file=sys.stderr)

    print("Running A3 (session-split diagnostic)...", file=sys.stderr)
    a3 = a3_session_split(a1)

    if a.json:
        print(json.dumps({"a1": a1, "a2": a2, "a3": a3}, indent=2, default=str))
        return 0

    _print_a1(a1)
    if a2 is not None:
        _print_a2(a2)
    _print_a3(a3)
    return 0


def _print_a1(a1):
    print("\n=== A1 — RVOL-quartile expectancy (floor 0.0, entry_mode close) ===")
    for trigger, res in sorted(a1.items()):
        print(f"\n-- {trigger} -- monotonicity: {res['monotonicity']}")
        for coin, cd in res["per_coin"].items():
            print(f"  {coin} (n={cd['n_total']}, excluded_no_rvol={cd['n_excluded_no_rvol']}):")
            for q in cd["quartiles"]:
                print(f"    {q['label']} rvol{q['rvol_range']} n{q['n']:<4} "
                      f"win{q['win_pct']:>5}% exp{q['net_exp']:+.3f}R tot{q['total_r']:+.1f}")
        pooled = res["pooled"]
        print(f"  POOLED (n={pooled['n_total']}, excluded_no_rvol={pooled['n_excluded_no_rvol']}):")
        for q in pooled["quartiles"]:
            print(f"    {q['label']} rvol{q['rvol_range']} n{q['n']:<4} "
                  f"win{q['win_pct']:>5}% exp{q['net_exp']:+.3f}R tot{q['total_r']:+.1f}")


def _print_a2(cells):
    print("\n=== A2 — threshold filters (floor 2.0) ===")
    passed = [c for c in cells if c["pass"]]
    print(f"  {len(cells)} cells total, {len(passed)} PASS")
    for c in sorted(cells, key=lambda x: (x["trigger"], x["mode"], x["family"], x["k"])):
        v = "PASS" if c["pass"] else "reject"
        print(f"  {c['trigger']:8} {c['mode']:6} {c['family']} k{c['k']:<4} "
              f"HYPE n{c['hype']['n']:<3} exp{c['hype']['net_exp']:+.3f}R | "
              f"pooledOOS n{c['pooled_oos']['n']:<4} exp{c['pooled_oos']['net_exp']:+.3f}R "
              f"| {v}" + (f" (failed: {','.join(c['failed'])})" if not c["pass"] else ""))


def _print_a3(a3):
    print("\n=== A3 — session-split diagnostic (report-only) ===")
    for trigger, sessions in sorted(a3.items()):
        print(f"\n-- {trigger} --")
        for sess, sd in sessions.items():
            print(f"  {sess:16} n{sd['n']:<4} win{sd['win_pct']:>5}% "
                  f"exp{sd['net_exp']:+.3f}R mean_rvol{sd['mean_rvol']}")


if __name__ == "__main__":
    sys.exit(main())
