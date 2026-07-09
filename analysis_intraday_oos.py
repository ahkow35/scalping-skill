"""Exp 3 + Exp 4 — intraday timeframe + out-of-sample validation
(see FINDINGS-live-bot-2026-07-10.md §4).

Exp 3: faithful scalp path (15m levels / 5m entry / same-UTC-day close) on one
coin, per trigger. long_A (flush-reclaim) surfaces positive on HYPE.
Exp 4: validate long_A across coins. It fails out-of-sample (overfit to HYPE).

Uses backtest.run_intraday (committed, unit-tested). Throttles multi-coin
fetches to avoid Hyperliquid HTTP 429.

Run:  python3 analysis_intraday_oos.py            # OOS across coins
      python3 analysis_intraday_oos.py --exp3 HYPE  # single-coin per-trigger
"""
import sys
import time

import backtest
from fetch_market import HL_INFO, _post_json

_MS = {"5m": 300_000, "15m": 900_000}


def _raw(coin, interval, s, e, delay=0.35, retries=6):
    for a in range(retries):
        try:
            rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval, "startTime": s, "endTime": e}},
                "hyperliquid")
            time.sleep(delay)
            return [{"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
                     "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
                    for k in rows or []]
        except Exception as ex:  # noqa: BLE001 — throttle/retry on 429
            if "429" in str(ex) or "Too Many" in str(ex):
                time.sleep(3 * (a + 1))
                continue
            raise
    return []


def _hist(coin, interval, days, now):
    step = _MS[interval]
    span = step * 400
    cur = now - days * 86_400_000
    seen = {}
    while cur < now:
        ch = _raw(coin, interval, cur, min(cur + span, now))
        if not ch:
            cur += span
            continue
        for c in ch:
            seen[c["t"]] = c
        cur = ch[-1]["t"] + step
    return [seen[t] for t in sorted(seen)]


def _stat(trades):
    nets = [t["net_r"] for t in trades if t["net_r"] is not None]
    n = len(nets)
    if not n:
        return (0, 0.0, 0.0, 0.0)
    return (n, round(sum(1 for r in nets if r > 0) / n * 100, 1),
            round(sum(nets) / n, 3), round(sum(nets), 1))


def exp3(coin, days=60):
    now = int(time.time() * 1000)
    c5, c15 = _hist(coin, "5m", days, now), _hist(coin, "15m", days, now)
    print(f"Exp 3 — {coin} {days}d intraday (15m levels / 5m entry / same-day)\n")
    for side in ("long", "short"):
        for floor in (1.5, 2.0):
            for letter in ("A", "B"):
                tr = backtest.run_intraday(side, c5, c15, allow=(letter,),
                                           trig_params={"min_net_rr_t1": floor})
                n, w, e, tot = _stat(tr)
                if n:
                    print(f"  {side} {side}_{letter} floor{floor}: n{n} win{w}% exp{e:+.3f} tot{tot:+.1f}")


def exp4(coins=("HYPE", "SOL", "INJ", "NEAR"), days=60):
    """§4b definitive rerun: all triggers × close-vs-retest × coins, equalized."""
    now = int(time.time() * 1000)
    data = {co: (_hist(co, "5m", days, now), _hist(co, "15m", days, now)) for co in coins}
    print(f"Exp 4b — OOS, {days}d intraday, floor 2.0, all coins equalized\n")
    for side, letter in (("long", "A"), ("long", "B"), ("short", "A"), ("short", "B")):
        for mode in ("close", "retest"):
            pool, cells = [], []
            for co in coins:
                c5, c15 = data[co]
                tr = backtest.run_intraday(side, c5, c15, allow=(letter,),
                                           trig_params={"entry_mode": mode})
                pool += tr
                n, _, e, _ = _stat(tr)
                cells.append(f"{co} n{n} {e:+.2f}")
            n, w, e, tot = _stat(pool)
            print(f"  {side}_{letter} {mode:6}: POOL n{n:<3} win{w:>5}% exp{e:+.3f}R "
                  f"tot{tot:+7.1f}  [{' | '.join(cells)}]")
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--exp3":
        exp3(sys.argv[2] if len(sys.argv) > 2 else "HYPE")
    else:
        exp4()
