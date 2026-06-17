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


HL_TAKER_FEE_BPS = 4.5  # Hyperliquid Tier 1 taker fee; reference buffer for microprice gate


def compute_microprice(bids, asks):
    """Stoikov (2018) simple imbalance-weighted microprice from top-of-book.

    micro = ask * (bid_sz / total) + bid * (ask_sz / total)

    Weight inverts intuitively: a big bid stack pulls fair value TOWARD the ask,
    because the ask is what gets lifted next. `microprice_dev_bps` is the
    actionable scalar: positive => book leans up, negative => book leans down.

    Top-of-book only (original Stoikov form). Not the multi-level weighted
    extension and not the full iterative martingale-corrected microprice — the
    simple form is the right primitive at retail REST/WS latency.

    Inputs are the raw `levels` from Hyperliquid l2Book — list of
    `{"px": "...", "sz": "..."}` strings. None returns mean L2 is empty or
    malformed; callers should fall through to the trigger price as written.
    """
    base = {"microprice": None, "mid_l1": None, "best_bid": None,
            "best_ask": None, "best_bid_sz": None, "best_ask_sz": None,
            "spread_bps": None, "microprice_dev_bps": None,
            "taker_fee_bps_reference": HL_TAKER_FEE_BPS}
    if not bids or not asks:
        return {**base, "note": "empty book"}
    try:
        bid = float(bids[0]["px"])
        ask = float(asks[0]["px"])
        bid_sz = float(bids[0]["sz"])
        ask_sz = float(asks[0]["sz"])
    except (KeyError, ValueError, TypeError, IndexError):
        return {**base, "note": "malformed level data"}

    total_sz = bid_sz + ask_sz
    mid = (bid + ask) / 2
    if total_sz <= 0 or mid <= 0:
        return {**base, "mid_l1": round(mid, 6) if mid > 0 else None,
                "best_bid": bid, "best_ask": ask,
                "best_bid_sz": bid_sz, "best_ask_sz": ask_sz,
                "note": "zero size or non-positive mid"}

    micro = ask * (bid_sz / total_sz) + bid * (ask_sz / total_sz)
    spread_bps = (ask - bid) / mid * 10000
    dev_bps = (micro - mid) / mid * 10000

    note = "positive dev = book leaning up (bid stack dominant); negative = leaning down"
    if bid >= ask:
        note = "crossed/locked book — microprice unreliable; " + note

    return {
        "microprice": round(micro, 6),
        "mid_l1": round(mid, 6),
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_sz": bid_sz,
        "best_ask_sz": ask_sz,
        "spread_bps": round(spread_bps, 2),
        "microprice_dev_bps": round(dev_bps, 2),
        "taker_fee_bps_reference": HL_TAKER_FEE_BPS,
        "note": note,
    }


import json
import os
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


_CG_HEADERS = {"User-Agent": "scalp-tool/1.0 (personal trading script)"}


def _cg_get_json(url, source):
    """CoinGecko GET with User-Agent header and 2 retries at 8s each."""
    last_exc = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=_CG_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read())
        except Exception as exc:
            last_exc = exc
    raise DataUnavailable(f"DATA UNAVAILABLE: {source} (<{last_exc}>)")


def parse_btc_dominance(payload):
    """Current BTC dominance from CoinGecko /global.

    CoinGecko's /global does NOT expose historical BTC.D change. The field
    `market_cap_change_percentage_24h_usd` is total-mcap change, not dominance
    change — a previous version of this function mislabeled it as
    `btc_d_24h_chg` and that bug propagated into the macro veto.

    The 24h change must be derived from a local rolling cache of BTC.D
    snapshots — see `update_btcd_cache` + `compute_btcd_24h_change`.
    """
    try:
        d = payload["data"]
        return {"btc_d": float(d["market_cap_percentage"]["btc"])}
    except (KeyError, TypeError, ValueError):
        raise DataUnavailable("DATA UNAVAILABLE: coingecko (/global shape)")


def fetch_btc_dominance():
    return parse_btc_dominance(_cg_get_json(CG_GLOBAL, "coingecko"))


BTCD_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".btcd_cache.jsonl")
BTCD_CACHE_MAX_AGE_MS = 48 * 60 * 60 * 1000          # keep 48h of snapshots
BTCD_TARGET_LOOKBACK_MS = 24 * 60 * 60 * 1000        # 24h target lookback
BTCD_TARGET_TOLERANCE_MS = 90 * 60 * 1000            # ±90min around the 24h mark
BTCD_STALE_FALLBACK_MS = 2 * 60 * 60 * 1000          # use cache as fallback if < 2h stale


def update_btcd_cache(btc_d, now_ms, path=None):
    """Append the current BTC.D snapshot to the local rolling cache.

    Cache is JSONL: one `{"ts": ms, "btc_d": pct}` per line. Rows older than
    BTCD_CACHE_MAX_AGE_MS are dropped on write. We build the BTC.D 24h-change
    signal ourselves across repeated /scalp calls because the upstream API
    does not provide it.

    Returns the merged on-disk history (sorted ascending by ts).
    """
    path = path or BTCD_CACHE_PATH
    cutoff = now_ms - BTCD_CACHE_MAX_AGE_MS
    rows = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(r.get("ts", 0)) < cutoff:
                        continue
                    rows.append(r)
        except OSError:
            pass

    rows.append({"ts": int(now_ms), "btc_d": float(btc_d)})
    rows.sort(key=lambda r: int(r["ts"]))

    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, path)
    return rows


def _read_latest_btcd(path=None):
    """Return the most recent cached BTC.D row {ts, btc_d}, or raise DataUnavailable."""
    path = path or BTCD_CACHE_PATH
    if not os.path.exists(path):
        raise DataUnavailable("DATA UNAVAILABLE: coingecko (no cache on disk)")
    best = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if best is None or int(r.get("ts", 0)) > int(best.get("ts", 0)):
                        best = r
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        raise DataUnavailable(f"DATA UNAVAILABLE: coingecko (cache read error: {exc})")
    if best is None:
        raise DataUnavailable("DATA UNAVAILABLE: coingecko (cache empty)")
    return best


def compute_btcd_24h_change(rows, now_ms, current_btcd):
    """Compute the BTC.D 24h change from cached snapshots.

    Picks the cached sample closest to (now − 24h) within ±90 min tolerance.
    Returns a dict with:
      - btc_d_24h_chg: signed % change, or None if no sample in window
      - btc_d_sample_age_min: age of the chosen sample, or None
      - btc_d_coverage_h: age of the oldest sample in cache (hours)

    `btc_d_24h_chg = None` is the "cache still warming up" signal — callers
    should NOT fire the BTC.D-based veto when it is None, but other macro
    checks (BTC structural break, funding extremes, event days) still apply.
    """
    target = now_ms - BTCD_TARGET_LOOKBACK_MS
    lo = target - BTCD_TARGET_TOLERANCE_MS
    hi = target + BTCD_TARGET_TOLERANCE_MS

    coverage_h = None
    if rows:
        oldest_ts = min(int(r["ts"]) for r in rows)
        coverage_h = round((now_ms - oldest_ts) / 3_600_000, 1)

    in_window = [r for r in rows if lo <= int(r["ts"]) <= hi]
    if not in_window:
        return {"btc_d_24h_chg": None,
                "btc_d_sample_age_min": None,
                "btc_d_coverage_h": coverage_h}

    sample = min(in_window, key=lambda r: abs(int(r["ts"]) - target))
    sample_btcd = float(sample["btc_d"])
    if sample_btcd == 0:
        return {"btc_d_24h_chg": None,
                "btc_d_sample_age_min": None,
                "btc_d_coverage_h": coverage_h}
    change_pct = (float(current_btcd) - sample_btcd) / sample_btcd * 100
    sample_age_min = round((now_ms - int(sample["ts"])) / 60_000, 1)
    return {"btc_d_24h_chg": round(change_pct, 3),
            "btc_d_sample_age_min": sample_age_min,
            "btc_d_coverage_h": coverage_h}


def parse_coin_arg(raw):
    """Parse a coin string into (canonical_coin, dex_or_None).

    Core perps (BTC, HYPE, ETH...) are uppercased and have no dex.
    HIP-3 perps are deployer-namespaced (e.g. "xyz:SPCX") — dex lowercased,
    base uppercased, joined with the colon preserved exactly as Hyperliquid
    stores them in the universe.

    Examples:
      parse_coin_arg("hype")      -> ("HYPE", None)
      parse_coin_arg("xyz:SPCX")  -> ("xyz:SPCX", "xyz")
      parse_coin_arg("XYZ:spcx")  -> ("xyz:SPCX", "xyz")
    """
    if ":" in raw:
        dex, base = raw.split(":", 1)
        return f"{dex.lower()}:{base.upper()}", dex.lower()
    return raw.upper(), None


def fetch_ctx(coin, dex=None):
    """Fetch market context for a perp. Pass dex='xyz' for HIP-3 builder dexes."""
    payload = {"type": "metaAndAssetCtxs"}
    if dex is not None:
        payload["dex"] = dex
    d = _post_json(HL_INFO, payload, "hyperliquid")
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
    where = f"dex={dex!r}" if dex else "core perps"
    raise DataUnavailable(f"DATA UNAVAILABLE: hyperliquid (coin {coin} not found in {where})")


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
    return {
        "asks": band_aggregate(asks),
        "bids": band_aggregate(bids),
        "execution": compute_microprice(bids, asks),
    }


def fetch_recent_trades(coin):
    """Recent taker trades. side 'B' = taker buy (lifted ask), 'A' = taker sell (hit bid).

    Hyperliquid hard-caps this endpoint at ~10 trades — see merge_trade_cache for
    how we build longer history across repeated calls.
    """
    return _post_json(HL_INFO, {"type": "recentTrades", "coin": coin}, "hyperliquid")


TRADE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trade_cache")
TRADE_CACHE_MAX_AGE_MS = 6 * 60 * 60 * 1000  # keep 6h of trades


def _trade_cache_path(coin):
    """Sanitize coin (e.g. 'xyz:SPCX') into a filesystem-safe cache filename."""
    return os.path.join(TRADE_CACHE_DIR, f"{coin.replace(':', '_')}.jsonl")


def merge_trade_cache(coin, fresh_trades, now_ms):
    """Dedupe fresh trades against on-disk cache, write merged back, return combined list.

    Cache lives at .trade_cache/<sanitized-coin>.jsonl (one trade per line). Trades
    older than TRADE_CACHE_MAX_AGE_MS are dropped on write. Dedupe key is `tid`. This
    is how we build a meaningful taker-delta history from the 10-trade recentTrades
    cap — across repeated /scalp calls (typical loop = every 5m) the cache accumulates.
    HIP-3 namespaced coins (e.g. "xyz:SPCX") have ':' replaced with '_' in the filename.
    """
    os.makedirs(TRADE_CACHE_DIR, exist_ok=True)
    path = _trade_cache_path(coin)
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

import regime as regime_mod

_NOW = lambda: int(_time.time() * 1000)
_DAY = 86400000


def assemble(coin, deep=False, now_ms=None):
    now_ms = _NOW() if now_ms is None else now_ms
    # Detect HIP-3 namespacing. Primary coin may live on a builder dex; BTC ctx for
    # macro veto is always on core. l2Book / candleSnapshot / recentTrades all accept
    # namespaced coin names directly with no dex param — only metaAndAssetCtxs needs it.
    _, primary_dex = parse_coin_arg(coin) if isinstance(coin, str) else (coin, None)
    out = {"session": build_session(now_ms), "primary": coin, "primary_dex": primary_dex}

    out["ctx"] = fetch_ctx(coin, dex=primary_dex)
    out["btc_ctx"] = fetch_ctx("BTC")

    # 1d always included (365d) — needed for ATH / discovery state detection.
    # Payload stays small (~one row per day) so the cost is negligible.
    # 15m window = 12h (~48 bars): the regime classifier needs >=20 bars for
    # range_compression and >=32 for btc_corr; the old 2.5h (~10 bars) left both
    # None -> regime always "unknown" -> passive fade_ok never armed.
    spans = [("1d", 365 * _DAY), ("1h", 3 * _DAY), ("15m", 43_200_000), ("5m", 5400000)]
    if deep:
        spans = spans[:1] + [("4h", 12 * _DAY)] + spans[1:]
    out["candles"] = {iv: fetch_candles(coin, iv, now_ms - span, now_ms)
                      for iv, span in spans}
    out["btc_candles"] = {iv: fetch_candles("BTC", iv, now_ms - span, now_ms)
                          for iv, span in (("4h", 12 * _DAY), ("1h", 3 * _DAY),
                                           ("15m", 43_200_000))}
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

    out["regime"] = regime_mod.classify(
        out["candles"], out["btc_candles"],
        out.get("taker_delta"), out.get("book"))

    _live_exc = None
    parsed_btcd = None
    btcd_source = "live"
    try:
        parsed_btcd = fetch_btc_dominance()
    except DataUnavailable as exc:
        _live_exc = exc
        # Fallback: use the most recent cached snapshot if < 2h stale
        try:
            cached = _read_latest_btcd()
            age_ms = now_ms - int(cached["ts"])
            if age_ms > BTCD_STALE_FALLBACK_MS:
                raise DataUnavailable(
                    f"DATA UNAVAILABLE: coingecko (cache {round(age_ms/3_600_000, 1)}h stale)"
                )
            parsed_btcd = {"btc_d": float(cached["btc_d"])}
            btcd_source = f"cache ({round(age_ms / 60_000)}min old — live: {_live_exc})"
        except DataUnavailable:
            pass

    if parsed_btcd is not None:
        rows = update_btcd_cache(parsed_btcd["btc_d"], now_ms)
        delta = compute_btcd_24h_change(rows, now_ms, parsed_btcd["btc_d"])
        out["btc_dominance"] = {
            "btc_d": parsed_btcd["btc_d"],
            "source": btcd_source,
            **delta,
            "_note": ("btc_d_24h_chg is derived from a LOCAL ROLLING CACHE of "
                      "BTC.D snapshots — CoinGecko's /global endpoint does not "
                      "expose historical BTC.D change. If btc_d_24h_chg is None "
                      "the cache hasn't yet accumulated a sample near 24h ago "
                      "(target ±90min); skip the BTC.D component of the macro "
                      "veto in that case — other macro checks (BTC structure, "
                      "funding, event days) still apply. btc_d_coverage_h is "
                      "the age of the oldest sample in cache."),
        }
        out["macro_can_clear"] = True
    else:
        out["btc_dominance"] = str(_live_exc)
        out["macro_can_clear"] = False
    return out


def main(argv):
    args = [a for a in argv[1:] if a != "--deep"]
    deep = "--deep" in argv[1:] or "deep" in args
    args = [a for a in args if a != "deep"]
    raw = args[0] if args else "HYPE"
    coin, _ = parse_coin_arg(raw)
    try:
        out = assemble(coin, deep=deep)
    except DataUnavailable as exc:
        print(str(exc))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
