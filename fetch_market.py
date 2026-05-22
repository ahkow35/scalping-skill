from datetime import datetime, timezone


def iso_utc(ms):
    """Format an epoch-ms timestamp as 'YYYY-MM-DD HH:MM UTC'.

    Always UTC regardless of machine timezone. This function exists to
    structurally prevent the +8h label bug (fromtimestamp without tz).
    """
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


from datetime import timedelta

SGT = timezone(timedelta(hours=8))


def build_session(now_ms):
    """Deterministic session/time facts. US cash open 13:30 UTC,
    close 20:00 UTC, econ-data window 12:30 UTC. Asia handoff = the
    2h after US close. All times UTC-anchored."""
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    us_open = day + timedelta(hours=13, minutes=30)
    us_close = day + timedelta(hours=20)
    econ = day + timedelta(hours=12, minutes=30)
    h_open = (us_open - now).total_seconds() / 3600
    h_close = (us_close - now).total_seconds() / 3600
    us_live = us_open <= now < us_close
    # weekend window: Fri 20:00 UTC -> Sun 20:00 UTC. weekday(): Mon=0 .. Sun=6.
    wd = now.weekday()
    weekend = (
        (wd == 4 and now.hour >= 20)   # Friday after 20:00
        or wd == 5                      # all Saturday
        or (wd == 6 and now.hour < 20)  # Sunday before 20:00
    )
    return {
        "utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "sgt": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
        "hours_to_us_open": round(h_open, 2),
        "hours_to_us_close": round(h_close, 2),
        "hours_to_econ_window": round((econ - now).total_seconds() / 3600, 2),
        "us_session_live": us_live,
        "asia_handoff_soon": us_close <= now < us_close + timedelta(hours=2),
        "weekend_window": weekend,
    }


from collections import defaultdict


def band_aggregate(levels, bucket=0.05):
    """Sum USDC notional (px*sz) into nearest price buckets of width `bucket`."""
    agg = defaultdict(float)
    inv = 1.0 / bucket
    for lv in levels:
        p = float(lv["px"])
        b = round(round(p * inv) / inv, 2)
        agg[b] += p * float(lv["sz"])
    return dict(agg)


import json
import urllib.request

HL_INFO = "https://api.hyperliquid.xyz/info"
CG_GLOBAL = "https://api.coingecko.com/api/v3/global"


class DataUnavailable(Exception):
    """Raised when a source fails. Never emit stale/estimated data."""


def _post_json(url, payload, source):
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as exc:
        raise DataUnavailable(f"DATA UNAVAILABLE: {source} ({exc})")


def _get_json(url, source):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as exc:
        raise DataUnavailable(f"DATA UNAVAILABLE: {source} ({exc})")


def parse_btc_dominance(payload):
    try:
        d = payload["data"]
        return {"btc_d": float(d["market_cap_percentage"]["btc"]),
                "btc_d_24h_chg": float(d["market_cap_change_percentage_24h_usd"])}
    except (KeyError, TypeError, ValueError):
        raise DataUnavailable("DATA UNAVAILABLE: coingecko (/global shape)")


def fetch_btc_dominance():
    return parse_btc_dominance(_get_json(CG_GLOBAL, "coingecko"))


def fetch_ctx(coin):
    d = _post_json(HL_INFO, {"type": "metaAndAssetCtxs"}, "hyperliquid")
    for i, u in enumerate(d[0]["universe"]):
        if u["name"] == coin:
            c = d[1][i]
            return {
                "coin": coin,
                "mark": float(c["markPx"]), "oracle": float(c["oraclePx"]),
                "mid": float(c["midPx"]), "funding": float(c["funding"]),
                "premium": float(c["premium"]),
                "oi_usdc": float(c["openInterest"]) * float(c["markPx"]),
                "prev_day_px": float(c["prevDayPx"]),
                "day_vol_usdc": float(c["dayNtlVlm"]),
            }
    raise DataUnavailable(f"DATA UNAVAILABLE: hyperliquid (coin {coin} not found)")


def fetch_candles(coin, interval, start_ms, end_ms):
    rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": start_ms, "endTime": end_ms}}, "hyperliquid")
    return [{"t": iso_utc(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
             "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
            for k in rows]


def compute_ath_state(candles_1d, mark):
    """Classify price relative to all-time high from 1d candles.

    States:
      below_ath          — more than 5% below ATH; standard structure analysis applies
      approaching_ath    — within 5% of ATH; ATH dominates as the supply zone
      at_ath_zone        — within 0.5% of ATH; first touch usually rejects, wait for
                           5m/15m close above + retest before treating as breakout
      above_ath_discovery — more than 0.5% above ATH; no overhead supply, target logic
                           switches to measured-move + round-number magnets
    """
    if not candles_1d:
        return {"state": "unknown", "ath_price": None,
                "note": "no 1d candles available"}
    ath_bar = max(candles_1d, key=lambda c: c["h"])
    ath_price = float(ath_bar["h"])
    ath_date = ath_bar["t"][:10] if isinstance(ath_bar.get("t"), str) else None
    dist_pct = (mark / ath_price - 1) * 100
    if dist_pct > 0.5:
        state = "above_ath_discovery"
    elif dist_pct >= -0.5:
        state = "at_ath_zone"
    elif dist_pct >= -5.0:
        state = "approaching_ath"
    else:
        state = "below_ath"
    return {
        "ath_price": ath_price,
        "ath_date": ath_date,
        "current_distance_pct": round(dist_pct, 2),
        "state": state,
    }


def fetch_l2(coin):
    d = _post_json(HL_INFO, {"type": "l2Book", "coin": coin}, "hyperliquid")
    bids, asks = d["levels"]
    return {"asks": band_aggregate(asks), "bids": band_aggregate(bids)}


def fetch_recent_trades(coin):
    """Recent taker trades. side 'B' = taker buy (lifted ask), 'A' = taker sell (hit bid).

    Hyperliquid hard-caps this endpoint at ~10 trades — see merge_trade_cache for
    how we build longer history across repeated calls.
    """
    return _post_json(HL_INFO, {"type": "recentTrades", "coin": coin}, "hyperliquid")


import os

TRADE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trade_cache")
TRADE_CACHE_MAX_AGE_MS = 6 * 60 * 60 * 1000  # keep 6h of trades


def merge_trade_cache(coin, fresh_trades, now_ms):
    """Dedupe fresh trades against on-disk cache, write merged back, return combined list.

    Cache lives at .trade_cache/<COIN>.jsonl (one trade per line). Trades older than
    TRADE_CACHE_MAX_AGE_MS are dropped on write. Dedupe key is `tid`. This is how we
    build a meaningful taker-delta history from the 10-trade recentTrades cap — across
    repeated /scalp calls (typical loop = every 5m) the cache accumulates.
    """
    os.makedirs(TRADE_CACHE_DIR, exist_ok=True)
    path = os.path.join(TRADE_CACHE_DIR, f"{coin.upper()}.jsonl")
    cutoff = now_ms - TRADE_CACHE_MAX_AGE_MS

    by_tid = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(t.get("time", 0)) < cutoff:
                        continue
                    by_tid[t.get("tid")] = t
        except OSError:
            pass

    for t in fresh_trades:
        if int(t.get("time", 0)) < cutoff:
            continue
        by_tid[t.get("tid")] = t

    merged = sorted(by_tid.values(), key=lambda x: int(x["time"]))

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for t in merged:
            f.write(json.dumps(t) + "\n")
    os.replace(tmp, path)

    return merged


def bucket_taker_delta(trades, now_ms,
                      windows=(("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240))):
    """Aggregate taker buy vs sell USDC notional into trailing windows ending at now_ms.

    Returns per-window dict with buy_usdc, sell_usdc, delta_usdc, buy_share_pct,
    trade_count, and coverage_pct (how much of the window the trade history covers).
    coverage_pct < 100 means recentTrades didn't reach far enough back — treat the
    bucket as partial.
    """
    if not trades:
        return {name: {"coverage_pct": 0.0, "trade_count": 0} for name, _ in windows}

    times = [int(t["time"]) for t in trades]
    earliest_ms = min(times)
    result = {}
    for name, mins in windows:
        win_ms = mins * 60 * 1000
        win_start = now_ms - win_ms
        buy_usdc = 0.0
        sell_usdc = 0.0
        count = 0
        for t in trades:
            t_ms = int(t["time"])
            if t_ms < win_start:
                continue
            notional = float(t["px"]) * float(t["sz"])
            count += 1
            if t["side"] == "B":
                buy_usdc += notional
            elif t["side"] == "A":
                sell_usdc += notional
        total = buy_usdc + sell_usdc
        # coverage: how much of the window the trade stream actually reaches
        if earliest_ms <= win_start:
            coverage = 100.0
        else:
            coverage = round(100.0 * (now_ms - earliest_ms) / win_ms, 1)
        result[name] = {
            "buy_usdc": round(buy_usdc, 2),
            "sell_usdc": round(sell_usdc, 2),
            "delta_usdc": round(buy_usdc - sell_usdc, 2),
            "buy_share_pct": round(100.0 * buy_usdc / total, 1) if total > 0 else None,
            "trade_count": count,
            "coverage_pct": max(0.0, min(100.0, coverage)),
        }
    return result


import sys
import time as _time

_NOW = lambda: int(_time.time() * 1000)
_DAY = 86400000


def assemble(coin, deep=False, now_ms=None):
    now_ms = _NOW() if now_ms is None else now_ms
    out = {"session": build_session(now_ms), "primary": coin}

    out["ctx"] = fetch_ctx(coin)
    out["btc_ctx"] = fetch_ctx("BTC")

    # 1d always included (365d) — needed for ATH / discovery state detection.
    # Payload stays small (~one row per day) so the cost is negligible.
    spans = [("1d", 365 * _DAY), ("1h", 3 * _DAY), ("15m", 9000000), ("5m", 5400000)]
    if deep:
        spans = spans[:1] + [("4h", 12 * _DAY)] + spans[1:]
    out["candles"] = {iv: fetch_candles(coin, iv, now_ms - span, now_ms)
                      for iv, span in spans}
    out["btc_candles"] = {iv: fetch_candles("BTC", iv, now_ms - span, now_ms)
                          for iv, span in (("4h", 12 * _DAY), ("1h", 3 * _DAY))}
    out["book"] = fetch_l2(coin)
    out["ath_state"] = compute_ath_state(out["candles"].get("1d", []), out["ctx"]["mark"])

    try:
        fresh = fetch_recent_trades(coin)
        merged = merge_trade_cache(coin, fresh, now_ms)
        out["taker_delta"] = bucket_taker_delta(merged, now_ms)
        out["taker_delta"]["_note"] = (
            f"Real taker aggressor delta from local cache ({len(merged)} trades, "
            "fed by repeated /scalp calls). side=B (taker bought) vs side=A (taker sold). "
            "coverage_pct<100 = window is partial — re-run /scalp every 5m to build history. "
            "Positive delta_usdc = buyers aggressive; negative = sellers aggressive."
        )
    except DataUnavailable as exc:
        out["taker_delta"] = str(exc)

    try:
        out["btc_dominance"] = fetch_btc_dominance()
        out["macro_can_clear"] = True
    except DataUnavailable as exc:
        out["btc_dominance"] = str(exc)
        out["macro_can_clear"] = False
    return out


def main(argv):
    args = [a for a in argv[1:] if a != "--deep"]
    deep = "--deep" in argv[1:] or "deep" in args
    args = [a for a in args if a != "deep"]
    coin = (args[0].upper() if args else "HYPE")
    try:
        out = assemble(coin, deep=deep)
    except DataUnavailable as exc:
        print(str(exc))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
