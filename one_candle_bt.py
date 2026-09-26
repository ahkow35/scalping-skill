"""Paper backtest of the "one-candle rule" (spec: one_candle_spec.md).

Usage: python3 one_candle_bt.py <dir of Binance HYPEUSDT 1m kline CSVs>
Paper-only research; never wired into /scalp.
"""

from __future__ import annotations

import glob
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import costs

NY = ZoneInfo("America/New_York")
BUF_STOP = 0.0002        # stop buffer beyond the candle body
BUF_SWING = 0.0005       # buffer below a 15m swing low
MIN_STOP = 0.0025        # minimum stop distance (fraction of price)
MIN_MOVE = 0.003         # intraday move beyond session open needed to arm
ENTRY_MIN, FLAT_MIN = 60, 150
H2_START = pd.Timestamp("2026-01-01", tz="UTC")
RNG = np.random.default_rng(20260925)


# ---------------------------------------------------------------- data
def load(dirpath: str) -> pd.DataFrame:
    frames = []
    for f in sorted(glob.glob(f"{dirpath}/*.csv")):
        d = pd.read_csv(f, header=None, usecols=range(5), dtype=str)
        d = d[pd.to_numeric(d[0], errors="coerce").notna()]   # drop header rows
        frames.append(d.astype(float))
    df = pd.concat(frames).rename(columns={0: "ts", 1: "o", 2: "h", 3: "l", 4: "c"})
    df["ts"] = df["ts"].astype(np.int64)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def data_report(df: pd.DataFrame) -> dict:
    gaps = np.diff(df["ts"].to_numpy()) // 60_000 - 1
    return {
        "first": str(pd.to_datetime(df.ts.iloc[0], unit="ms", utc=True)),
        "last": str(pd.to_datetime(df.ts.iloc[-1], unit="ms", utc=True)),
        "bars": len(df),
        "missing_minutes": int(gaps[gaps > 0].sum()),
        "largest_gap_min": int(gaps.max()),
    }


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    g = df.assign(k=df.ts // (minutes * 60_000)).groupby("k")
    out = g.agg(ts=("ts", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
    out["ts"] = out.index * minutes * 60_000
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- daily trend
def daily_trend(df: pd.DataFrame) -> dict[date, int]:
    """+1 up / -1 down / 0 none, using only daily bars closed before each date."""
    d = resample(df, 1440)
    days = [pd.to_datetime(t, unit="ms", utc=True).date() for t in d.ts]
    h, l = d.h.to_numpy(), d.l.to_numpy()
    sh, sl = [], []   # (confirm_date, price)
    for i in range(2, len(d) - 2):
        conf = days[i + 2]
        if h[i] > h[i - 2:i].max() and h[i] > h[i + 1:i + 3].max():
            sh.append((conf, h[i]))
        if l[i] < l[i - 2:i].min() and l[i] < l[i + 1:i + 3].min():
            sl.append((conf, l[i]))
    out = {}
    for day in days:
        hs = [p for c, p in sh if c < day][-2:]
        ls = [p for c, p in sl if c < day][-2:]
        if len(hs) < 2 or len(ls) < 2:
            out[day] = 0
        elif hs[1] > hs[0] and ls[1] > ls[0]:
            out[day] = 1
        elif hs[1] < hs[0] and ls[1] < ls[0]:
            out[day] = -1
        else:
            out[day] = 0
    return out


# ---------------------------------------------------------------- 15m swing lows
def swing15(df: pd.DataFrame):
    """Confirmed 15m pivot lows / highs as (confirm_ts_ms, price) arrays."""
    b = resample(df, 15)
    ts, h, l = b.ts.to_numpy(), b.h.to_numpy(), b.l.to_numpy()
    lows, highs = [], []
    for i in range(2, len(b) - 2):
        conf = ts[i + 2] + 15 * 60_000
        if l[i] < l[i - 2:i].min() and l[i] < l[i + 1:i + 3].min():
            lows.append((conf, l[i]))
        if h[i] > h[i - 2:i].max() and h[i] > h[i + 1:i + 3].max():
            highs.append((conf, h[i]))
    return np.array(lows), np.array(highs)


# ---------------------------------------------------------------- session engine
def session(ts, o, h, l, c, s, n_entry, n_flat, step_ms, direction, exit_mode, swings,
            placebo=None):
    """Simulate one session starting at index s. Returns a trade dict or None.

    Shorts are simulated by mirroring prices (p -> -p), so the long logic serves both.
    placebo=(entry_offset, stop_pct): enter at that bar's open with that stop, fixed2R.
    """
    sign = direction
    if sign == -1:
        o, h, l, c = -o, -l, -h, -c
    end = min(s + n_flat, len(ts))
    sess_open = o[s]
    ref_top = ref_bot = None
    armed = False
    pos = None
    trail_top = trail_bot = None
    for j in range(s, end):
        if pos is None:
            if placebo is not None:
                if j == s + placebo[0]:
                    e = o[j]
                    risk = abs(e) * placebo[1]
                    pos = dict(entry=e, stop=e - risk, risk=risk, target=e + 2 * risk, j=j)
            elif armed and j < s + n_entry:
                if l[j] <= ref_top:
                    fill = min(ref_top, o[j])
                    stop = ref_bot - BUF_STOP * abs(ref_bot)
                    if fill > stop and (fill - stop) >= MIN_STOP * abs(fill):
                        risk = fill - stop
                        pos = dict(entry=fill, stop=stop, risk=risk, target=fill + 2 * risk, j=j)
                        if l[j] <= stop:   # stopped on the fill bar
                            return _close(pos, stop, ts[j], sign, ambiguous=False, how="stop")
                    else:
                        ref_top = ref_bot = None
                        armed = False
            if pos is None:
                # update the reference candle with this closed bar
                if c[j] < o[j] and (ref_top is None or o[j] > ref_top):
                    ref_top, ref_bot, armed = o[j], c[j], False
                elif (ref_top is not None and not armed and c[j] > ref_top
                      and c[j] >= sess_open + MIN_MOVE * abs(sess_open)):
                    armed = True
                continue
            if placebo is None:
                continue   # fill bar handled; exits start next bar
        if j > pos["j"] or placebo is not None:
            hit_stop = l[j] <= pos["stop"]
            hit_tgt = exit_mode == "fixed2R" and h[j] >= pos["target"]
            if hit_stop:
                px = min(pos["stop"], o[j])
                return _close(pos, px, ts[j], sign, ambiguous=hit_tgt, how="stop")
            if hit_tgt:
                return _close(pos, max(pos["target"], o[j]), ts[j], sign, ambiguous=False, how="target")
        # trailing updates from this closed bar
        if exit_mode == "candle-trail":
            if c[j] < o[j] and (trail_top is None or o[j] > trail_top):
                trail_top, trail_bot = o[j], c[j]
            elif trail_top is not None and c[j] > trail_top:
                new = trail_bot - BUF_STOP * abs(trail_bot)
                if new > pos["stop"]:
                    pos["stop"] = new
        elif exit_mode == "swing15-trail":
            arr = swings[0] if sign == 1 else swings[1]
            t_close = ts[j] + step_ms
            k = np.searchsorted(arr[:, 0], t_close, side="right") - 1
            if k >= 0:
                px = arr[k, 1] * sign
                new = px - BUF_SWING * abs(px)
                if new > pos["stop"] and new < c[j]:
                    pos["stop"] = new
    if pos is not None:
        return _close(pos, c[end - 1], ts[end - 1], sign, ambiguous=False, how="time")
    return None


def _close(pos, px, t, sign, ambiguous, how):
    e = abs(pos["entry"])
    gross = (px - pos["entry"])          # mirrored: positive = profit either side
    cost = costs.COST_PER_FILL * costs.ROUND_TRIP_FILLS * e
    return dict(ts=int(t), side="long" if sign == 1 else "short",
                entry=e, stop_pct=pos["risk"] / e,
                net_r=(gross - cost) / pos["risk"], cost_r=cost / pos["risk"],
                ambiguous=ambiguous, exit=how)


# ---------------------------------------------------------------- session anchors
def ny_open_ms(day: date) -> int:
    return int(datetime.combine(day, time(9, 30), NY).timestamp() * 1000)


def anchors(days, mode):
    out = []
    for d in days:
        if d.weekday() >= 5:
            continue
        ny = ny_open_ms(d)
        if mode == "ny":
            out.append((d, ny))
        else:
            base = int(datetime.combine(d, time(0), timezone.utc).timestamp() * 1000)
            for hr in range(24):
                a = base + hr * 3_600_000
                if abs(a - ny) >= 3 * 3_600_000:
                    out.append((d, a))
    return out


def run(df, trend, swings, *, window, use_trend, exit_mode, tf=1, placebo_src=None, seed=None):
    bars = df if tf == 1 else resample(df, tf)
    ts, o, h, l, c = (bars[k].to_numpy() for k in ("ts", "o", "h", "l", "c"))
    step = tf * 60_000
    n_entry, n_flat = ENTRY_MIN // tf, FLAT_MIN // tf
    days = sorted(set(pd.to_datetime(ts, unit="ms", utc=True).date))
    trades = []
    rng = np.random.default_rng(seed) if seed is not None else None
    for d, a in anchors(days, window):
        s = np.searchsorted(ts, a)
        if s >= len(ts) or ts[s] != a:
            continue
        t = trend.get(d, 0)
        if placebo_src is not None:
            src = placebo_src.get(a)
            if src is None:
                continue
            tr = session(ts, o, h, l, c, s, n_entry, n_flat, step, 1 if src["side"] == "long" else -1,
                         "fixed2R", swings, placebo=(int(rng.integers(0, n_entry)), src["stop_pct"]))
        elif use_trend:
            if t == 0:
                continue
            tr = session(ts, o, h, l, c, s, n_entry, n_flat, step, t, exit_mode, swings)
        else:
            cands = [x for x in (session(ts, o, h, l, c, s, n_entry, n_flat, step, dr, exit_mode, swings)
                                 for dr in (1, -1)) if x]
            tr = min(cands, key=lambda x: x["ts"]) if cands else None
        if tr:
            tr["anchor"] = a
            trades.append(tr)
    return trades


# ---------------------------------------------------------------- stats
def stats(trades):
    if not trades:
        return {"n": 0}
    t = pd.DataFrame(trades)
    t["day"] = pd.to_datetime(t.anchor, unit="ms", utc=True).dt.date
    r = t.net_r.to_numpy()
    by_day = [g.net_r.to_numpy() for _, g in t.groupby("day")]
    boots = []
    for _ in range(2000):
        pick = RNG.integers(0, len(by_day), len(by_day))
        allr = np.concatenate([by_day[i] for i in pick])
        boots.append(allr.mean())
    anchor_ts = pd.to_datetime(t.anchor, unit="ms", utc=True)
    h1, h2 = t[anchor_ts < H2_START].net_r, t[anchor_ts >= H2_START].net_r
    return {
        "n": len(t), "win_pct": round(100 * (r > 0).mean(), 1),
        "mean_r": round(r.mean(), 3), "median_r": round(float(np.median(r)), 3),
        "total_r": round(r.sum(), 1),
        "ci95": [round(float(np.percentile(boots, 2.5)), 3), round(float(np.percentile(boots, 97.5)), 3)],
        "mean_cost_r": round(t.cost_r.mean(), 3), "median_stop_pct": round(100 * t.stop_pct.median(), 3),
        "ambiguous": int(t.ambiguous.sum()),
        "h1": {"n": len(h1), "mean_r": round(h1.mean(), 3) if len(h1) else None},
        "h2": {"n": len(h2), "mean_r": round(h2.mean(), 3) if len(h2) else None},
        "exits": t.exit.value_counts().to_dict(),
    }


def main():
    df = load(sys.argv[1])
    trend = daily_trend(df)
    swings = swing15(df)
    res = {"data": data_report(df),
           "trend_days": {k: sum(1 for v in trend.values() if v == k) for k in (1, -1, 0)}}
    variants = {
        "V1": dict(window="ny", use_trend=True, exit_mode="fixed2R"),
        "V2": dict(window="ny", use_trend=True, exit_mode="candle-trail"),
        "V3": dict(window="ny", use_trend=True, exit_mode="swing15-trail"),
        "V4": dict(window="ny", use_trend=False, exit_mode="fixed2R"),
        "V5": dict(window="control", use_trend=True, exit_mode="fixed2R"),
        "V6": dict(window="control", use_trend=True, exit_mode="candle-trail"),
        "V8": dict(window="ny", use_trend=True, exit_mode="fixed2R", tf=5),
    }
    trades = {}
    for k, v in variants.items():
        trades[k] = run(df, trend, swings, **v)
        res[k] = stats(trades[k])
        print(k, json.dumps(res[k]), flush=True)
    src = {t["anchor"]: t for t in trades["V1"]}
    means = []
    for seed in range(200):
        pt = run(df, trend, swings, window="ny", use_trend=True, exit_mode="fixed2R",
                 placebo_src=src, seed=seed)
        means.append(np.mean([x["net_r"] for x in pt]))
    res["V7"] = {"seeds": 200, "n_per_seed": len(src),
                 "mean_of_means": round(float(np.mean(means)), 3),
                 "range95": [round(float(np.percentile(means, 2.5)), 3),
                             round(float(np.percentile(means, 97.5)), 3)]}
    print("V7", json.dumps(res["V7"]))
    print(json.dumps(res, indent=1, default=str), file=open("one_candle_results.json", "w"))


if __name__ == "__main__":
    main()
