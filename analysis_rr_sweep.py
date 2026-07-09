"""Exp 2 — R:R floor sweep (see FINDINGS-live-bot-2026-07-10.md §4).

Does loosening the net-R:R admission floor rescue the (negative) triggers?
Answer: no — loosening loses more; the problem is entry timing, not R:R.

Run from the repo dir:  python3 analysis_rr_sweep.py [COIN] [DAYS]
"""
import sys
import time

import backtest


def main(coin="HYPE", days=90):
    now = int(time.time() * 1000)
    candles = backtest.fetch_history(coin, "15m", days, now)
    print(f"{coin} {days}d 15m: {len(candles)} candles\n")
    for side in ("long", "short"):
        print(f"=== {side.upper()} — R:R floor sweep ===")
        for floor in (0.0, 1.0, 1.5, 2.0, 2.5, 3.0):
            trades = backtest.run(side, candles, trig_params={"min_net_rr_t1": floor})
            nets = [t["net_r"] for t in trades if t["net_r"] is not None]
            n = len(nets)
            wr = round(sum(1 for r in nets if r > 0) / n * 100, 1) if n else 0.0
            exp = round(sum(nets) / n, 3) if n else 0.0
            tot = round(sum(nets), 1) if n else 0.0
            print(f"  floor {floor}: n {n:3d} | win {wr:5.1f}% | exp {exp:+.3f}R | total {tot:+.1f}R")
        print()


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "HYPE", int(a[2]) if len(a) > 2 else 90)
