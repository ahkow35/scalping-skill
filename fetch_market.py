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
    return {
        "utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "sgt": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
        "hours_to_us_open": round(h_open, 2),
        "hours_to_us_close": round(h_close, 2),
        "hours_to_econ_window": round((econ - now).total_seconds() / 3600, 2),
        "us_session_live": us_live,
        "asia_handoff_soon": us_close <= now < us_close + timedelta(hours=2),
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


def fetch_l2(coin):
    d = _post_json(HL_INFO, {"type": "l2Book", "coin": coin}, "hyperliquid")
    bids, asks = d["levels"]
    return {"asks": band_aggregate(asks), "bids": band_aggregate(bids)}


import sys
import time as _time

_NOW = lambda: int(_time.time() * 1000)
_DAY = 86400000


def assemble(coin, deep=False, now_ms=None):
    now_ms = _NOW() if now_ms is None else now_ms
    out = {"session": build_session(now_ms), "primary": coin}

    out["ctx"] = fetch_ctx(coin)
    out["btc_ctx"] = fetch_ctx("BTC")

    spans = [("1h", 3 * _DAY), ("15m", 9000000), ("5m", 5400000)]
    if deep:
        spans = [("1d", 35 * _DAY), ("4h", 12 * _DAY)] + spans
    out["candles"] = {iv: fetch_candles(coin, iv, now_ms - span, now_ms)
                      for iv, span in spans}
    out["btc_candles"] = {iv: fetch_candles("BTC", iv, now_ms - span, now_ms)
                          for iv, span in (("4h", 12 * _DAY), ("1h", 3 * _DAY))}
    out["book"] = fetch_l2(coin)

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
