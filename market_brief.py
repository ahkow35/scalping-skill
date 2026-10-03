"""Read-only market-liquidity briefing for the Railway watcher.

Describes market CONDITIONS (how deep the book is, how crowded positioning
is, what events are coming) — never direction, and never an entry signal.
It places, cancels and closes nothing and never reads the account: the only
Hyperliquid calls are the PUBLIC market reads `metaAndAssetCtxs` and
`l2Book`. Other sources are free and keyless (DefiLlama, FRED CSV, Yahoo,
SoSoValue, ForexFactory); requests + stdlib only.

Every source fails on its own: a failure becomes an "Unavailable: <source>"
line and never stops the rest. Each fetch function takes injectable HTTP
callables, so tests run on saved fixtures with no network.

`python3 market_brief.py --print` prints a live briefing locally.
"""

import csv
import io
import json
import logging
import os
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("market_brief")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LLAMA_URL = "https://stablecoins.llama.fi/stablecoins"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
# Official FRED API, used when FRED_API_KEY is set: the CSV download above
# times out from Railway's servers (2026-10-03) while this host answers.
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_API_LOOKBACK_DAYS = 60
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
ETF_URL = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

HTTP_TIMEOUT_S = 8.0
USER_AGENT = "Mozilla/5.0 (compatible; scalp-market-brief)"
DEPTH_SIG_FIGS = (4, 3, 2)              # l2Book aggregations tried for the ±1% depth, finest first
BRIEF_TIMEZONE = "Asia/Singapore"
TZ_LABEL = "SGT"
MAX_TEXT = 4000

DEFAULT_TIMES = "08:00,20:30"
DEFAULT_COINS = "BTC,ETH,HYPE"
DEFAULT_FUNDING_ALERT_PCT = 0.005       # hourly funding, in percent
DEFAULT_OI_DROP_PCT = 5.0               # open-interest drop within 1 hour
DEFAULT_EVENT_MINUTES = 60
ALERT_REPEAT_S = 4 * 3600

SAMPLE_INTERVAL_S = 15 * 60
OI_RETENTION_S = 25 * 3600
OI_TOLERANCE_S = 2 * 60
LATE_GRACE_S = 2 * 60                   # beyond this after the slot, the message says "(late)"
LATE_LIMIT_S = 30 * 60                  # beyond this, the slot is skipped
TICK_INTERVAL_S = 30
CACHE_MAX_AGE_S = 5 * 60
COLLECT_DEADLINE_S = 20                 # whole-collection cap; a slower source is "Unavailable"
LIQUIDITY_PENDING_TEXT = "Building a fresh liquidity briefing — it will follow in a moment."

STATE_FILE = "market_brief_state.json"
OI_FILE = "market_brief_oi.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _float_env(env, name, default):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number; using the default %s", name, default)
        return default
    return value if value > 0 else default


def _parse_times(raw):
    times = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except ValueError:
            logger.warning("ignoring bad BRIEF_TIMES entry %r", part)
            continue
        times.append(f"{int(hh):02d}:{int(mm):02d}")
    return times


class BriefConfig:
    def __init__(self, *, enabled=True, times=None, coins=None,
                 funding_alert_pct=DEFAULT_FUNDING_ALERT_PCT, oi_drop_pct=DEFAULT_OI_DROP_PCT,
                 event_minutes=DEFAULT_EVENT_MINUTES):
        self.enabled = enabled
        self.times = times if times is not None else _parse_times(DEFAULT_TIMES)
        self.coins = coins if coins is not None else DEFAULT_COINS.split(",")
        self.funding_alert_pct = funding_alert_pct
        self.oi_drop_pct = oi_drop_pct
        self.event_minutes = event_minutes

    @classmethod
    def from_env(cls, env=os.environ):
        enabled = env.get("BRIEF_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
        times = _parse_times(env.get("BRIEF_TIMES", DEFAULT_TIMES)) or _parse_times(DEFAULT_TIMES)
        coins = [c.strip().upper() for c in env.get("BRIEF_COINS", DEFAULT_COINS).split(",") if c.strip()]
        return cls(
            enabled=enabled, times=times, coins=coins or DEFAULT_COINS.split(","),
            funding_alert_pct=_float_env(env, "BRIEF_FUNDING_ALERT_PCT", DEFAULT_FUNDING_ALERT_PCT),
            oi_drop_pct=_float_env(env, "BRIEF_OI_DROP_PCT", DEFAULT_OI_DROP_PCT),
            event_minutes=_float_env(env, "BRIEF_EVENT_MINUTES", DEFAULT_EVENT_MINUTES),
        )


# ---------------------------------------------------------------------------
# Fetchers — one per source. Each takes injectable HTTP callables, returns
# parsed figures, and raises on any failure (collect() turns that into an
# "Unavailable" line).
# ---------------------------------------------------------------------------

def _get(get, url, **kwargs):
    response = get(url, timeout=HTTP_TIMEOUT_S, headers={"User-Agent": USER_AGENT}, **kwargs)
    response.raise_for_status()
    return response


def _hl_post(post, body):
    response = post(HL_INFO_URL, json=body, timeout=HTTP_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def fetch_hl_markets(coins, post=requests.post):
    """Public Hyperliquid read: funding (hourly, as a fraction), open
    interest (contracts), mark price and 24h notional volume per coin."""
    meta, ctxs = _hl_post(post, {"type": "metaAndAssetCtxs"})
    index = {asset["name"]: i for i, asset in enumerate(meta["universe"])}
    out = {}
    for coin in coins:
        if coin not in index:
            raise ValueError(f"{coin} not in the Hyperliquid universe")
        ctx = ctxs[index[coin]]
        out[coin] = {
            "funding": float(ctx["funding"]),
            "oi": float(ctx["openInterest"]),
            "mark": float(ctx["markPx"]),
            "volume_24h": float(ctx["dayNtlVlm"]),
        }
    return out


def book_depth(book, band=0.01, mid=None):
    """Depth within +-`band` of the mid, in USD, and the spread in bp.
    `mid` defaults to this book's own mid; pass the full-precision mid when
    `book` is an aggregated one, whose top levels are rounded. `truncated`
    is True when the 20 levels l2Book returns do not reach the band edge,
    so the depth is a lower bound."""
    bids, asks = book["levels"]
    best_bid, best_ask = float(bids[0]["px"]), float(asks[0]["px"])
    if mid is None:
        mid = (best_bid + best_ask) / 2
    low, high = mid * (1 - band), mid * (1 + band)
    bid_usd = sum(float(lvl["px"]) * float(lvl["sz"]) for lvl in bids if float(lvl["px"]) >= low)
    ask_usd = sum(float(lvl["px"]) * float(lvl["sz"]) for lvl in asks if float(lvl["px"]) <= high)
    truncated = float(bids[-1]["px"]) >= low or float(asks[-1]["px"]) <= high
    return {"depth_usd": bid_usd + ask_usd, "spread_bp": (best_ask - best_bid) / mid * 1e4,
            "mid": mid, "truncated": truncated}


def fetch_hl_depth(coin, post=requests.post):
    """Spread from the full-precision book; depth from the finest aggregated
    book (nSigFigs 4, then 3, then 2) whose 20 levels reach the +-1% band.
    Aggregated levels are price buckets, so the depth is accurate to within
    one bucket at each band edge (about 0.1% of price at 3 significant
    figures). If no aggregation reaches the band, the lower bound from the
    coarsest one is returned with `truncated` set."""
    full = book_depth(_hl_post(post, {"type": "l2Book", "coin": coin}))
    for sig_figs in DEPTH_SIG_FIGS:
        book = _hl_post(post, {"type": "l2Book", "coin": coin, "nSigFigs": sig_figs})
        depth = book_depth(book, mid=full["mid"])
        if not depth["truncated"]:
            break
    return {**full, "depth_usd": depth["depth_usd"], "truncated": depth["truncated"]}


def fetch_stablecoins(get=requests.get):
    assets = _get(get, LLAMA_URL).json()["peggedAssets"]
    total = prev_week = 0.0
    for symbol in ("USDT", "USDC"):
        asset = next(a for a in assets if a["symbol"] == symbol)
        total += float(asset["circulating"]["peggedUSD"])
        prev_week += float(asset["circulatingPrevWeek"]["peggedUSD"])
    return {"total_usd": total, "week_change_usd": total - prev_week}


def parse_fred_csv(text):
    """[(date, value)] oldest first; FRED's '.' (no observation) is skipped."""
    rows = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) == 2 and row[1].strip() not in ("", "."):
            rows.append((row[0], float(row[1])))
    if not rows:
        raise ValueError("no FRED observations")
    return rows


def _as_of(rows, date):
    best = None
    for d, v in rows:
        if d <= date:
            best = v
    if best is None:
        raise ValueError(f"no observation on or before {date}")
    return best


def net_liquidity(walcl, tga, rrp):
    """Fed net liquidity in $B = balance sheet - Treasury cash - reverse repo,
    at the last two balance-sheet dates. Units: WALCL and WTREGEN are in
    MILLIONS, RRPONTSYD is in BILLIONS."""
    if len(walcl) < 2:
        raise ValueError("need two WALCL observations")
    values = []
    for date, wal in walcl[-2:]:
        values.append(wal / 1000 - _as_of(tga, date) / 1000 - _as_of(rrp, date))
    return {"net_b": values[1], "week_change_b": values[1] - values[0], "as_of": walcl[-1][0]}


def parse_fred_api(body):
    """[(date, value)] oldest first, from the FRED API's observations JSON;
    '.' (no observation) is skipped, as in parse_fred_csv."""
    rows = [(o["date"], float(o["value"])) for o in body["observations"]
            if str(o.get("value", "")).strip() not in ("", ".")]
    if not rows:
        raise ValueError("no FRED observations")
    return sorted(rows)


def _fred_series(get, series, api_key):
    if not api_key:
        return parse_fred_csv(_get(get, FRED_URL.format(series=series)).text)
    start = (datetime.now(ZoneInfo("UTC")) - timedelta(days=FRED_API_LOOKBACK_DAYS)).date().isoformat()
    # The key travels in the query string, so it is in the URL of any HTTP
    # error: collect() logs only the exception's class, never its text.
    params = {"series_id": series, "api_key": api_key, "file_type": "json", "observation_start": start}
    return parse_fred_api(_get(get, FRED_API_URL, params=params).json())


def fetch_net_liquidity(get=requests.get, api_key=None):
    """From the FRED API when FRED_API_KEY is set, else the keyless CSV."""
    api_key = os.environ.get("FRED_API_KEY", "").strip() if api_key is None else api_key
    series = {s: _fred_series(get, s, api_key) for s in ("WALCL", "WTREGEN", "RRPONTSYD")}
    return net_liquidity(series["WALCL"], series["WTREGEN"], series["RRPONTSYD"])


def parse_yahoo(body):
    result = body["chart"]["result"][0]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    return {"price": float(result["meta"]["regularMarketPrice"]), "prev": float(closes[-2])}


def fetch_yahoo(symbol, get=requests.get):
    return parse_yahoo(_get(get, YAHOO_URL.format(symbol=symbol)).json())


def parse_etf(body):
    rows = body["data"]
    latest = max(rows, key=lambda r: r["date"])
    return {"date": latest["date"], "net_inflow_usd": float(latest["totalNetInflow"])}


def fetch_etf_flow(post=requests.post):
    response = post(ETF_URL, json={"type": "us-btc-spot"}, timeout=HTTP_TIMEOUT_S,
                    headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return parse_etf(response.json())


def parse_events(body):
    """High-impact USD events as [(start datetime, title)], soonest first."""
    events = []
    for item in body:
        if item.get("country") == "USD" and item.get("impact") == "High":
            events.append((datetime.fromisoformat(item["date"]), item["title"]))
    return sorted(events)


def fetch_events(get=requests.get):
    return parse_events(_get(get, CALENDAR_URL).json())


# ---------------------------------------------------------------------------
# Collect — run every source independently
# ---------------------------------------------------------------------------

class SourcePool:
    """Runs each source call on its own daemon thread, with at most one call
    running per source. A source whose previous call has not finished is not
    called again — it is reported unavailable — so a server that trickles
    bytes past the per-read HTTP timeout holds one thread for that source
    only: the thread count never exceeds the number of sources, one hung
    source cannot take a slot another source needs, and a stuck call never
    blocks the process from exiting (and Railway from restarting it)."""

    def __init__(self, name="brief-source"):
        self._name = name
        self._running = {}  # source name -> Future of its last call
        self._lock = threading.Lock()

    def submit(self, name, fn):
        """The source's Future, or None while its previous call still runs."""
        with self._lock:
            previous = self._running.get(name)
            if previous is not None and not previous.done():
                return None
            future = self._running[name] = Future()

        def run():
            future.set_running_or_notify_cancel()
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)
        try:
            threading.Thread(target=run, daemon=True, name=f"{self._name}:{name}").start()
        except Exception as exc:  # e.g. thread exhaustion: fail this call, not the source forever
            future.set_running_or_notify_cancel()
            future.set_exception(exc)
        return future


_SOURCES = SourcePool()


def collect(config, *, get=requests.get, post=requests.post, deadline_s=COLLECT_DEADLINE_S,
            pool=None):
    """Fetch every source in parallel. Returns a dict of results; a source
    that raised, ran past the deadline or is still running from an earlier
    call is None and its name is listed in data["unavailable"]."""
    pool = pool or _SOURCES
    jobs = {
        "Hyperliquid markets": lambda: fetch_hl_markets(config.coins, post),
        "Stablecoins (DefiLlama)": lambda: fetch_stablecoins(get),
        "Fed net liquidity (FRED)": lambda: fetch_net_liquidity(get),
        "VIX (Yahoo)": lambda: fetch_yahoo("^VIX", get),
        "Dollar index (Yahoo)": lambda: fetch_yahoo("DX-Y.NYB", get),
        "US 10y (Yahoo)": lambda: fetch_yahoo("^TNX", get),
        "BTC ETF flow (SoSoValue)": lambda: fetch_etf_flow(post),
        "Economic calendar (ForexFactory)": lambda: fetch_events(get),
    }
    for coin in config.coins:
        jobs[f"Hyperliquid depth {coin}"] = (lambda c=coin: fetch_hl_depth(c, post))
    results, unavailable = {}, []
    futures = {name: pool.submit(name, fn) for name, fn in jobs.items()}
    deadline = time.monotonic() + deadline_s
    for name, future in futures.items():
        try:
            if future is None:
                raise TimeoutError("previous call still running")
            results[name] = future.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception as exc:
            logger.warning("source unavailable: %s (%s)", name, type(exc).__name__)
            results[name] = None
            unavailable.append(name)
    return {"results": results, "unavailable": unavailable}


# ---------------------------------------------------------------------------
# Open-interest history (Hyperliquid has no OI history endpoint)
# ---------------------------------------------------------------------------

def _write_json(path, payload):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


class OiHistory:
    """Open-interest samples per coin, kept 25 hours in a small JSON file so
    they survive restarts. A change needs a sample about `window` old (no more
    than one sampling interval beyond it); until then it is None ("warming
    up")."""

    def __init__(self, path):
        self._path = Path(path)
        data = _read_json(self._path, {})
        self._samples = data if isinstance(data, dict) else {}

    def add(self, now_ms, oi_by_coin):
        cutoff = now_ms - OI_RETENTION_S * 1000
        for coin, oi in oi_by_coin.items():
            kept = [s for s in self._samples.get(coin, []) if s[0] >= cutoff]
            kept.append([now_ms, oi])
            self._samples[coin] = kept
        _write_json(self._path, self._samples)

    def change_pct(self, coin, now_ms, window_s):
        # The baseline must sit near the window's start: a sample from before
        # a gap in sampling (e.g. a restart) would stretch "the last hour".
        limit = now_ms - (window_s - OI_TOLERANCE_S) * 1000
        oldest = now_ms - (window_s + SAMPLE_INTERVAL_S + OI_TOLERANCE_S) * 1000
        older = [s for s in self._samples.get(coin, []) if oldest <= s[0] <= limit]
        current = [s for s in self._samples.get(coin, []) if s[0] <= now_ms]
        if not older or not current or older[-1][1] <= 0:
            return None
        return (current[-1][1] - older[-1][1]) / older[-1][1] * 100


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _signed(value, fmt):
    return ("+" if value >= 0 else "-") + format(abs(value), fmt)


def _money(usd):
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(usd) >= limit:
            return f"${abs(usd) / limit:.2f}{suffix}" if suffix == "T" else f"${abs(usd) / limit:.1f}{suffix}"
    return f"${abs(usd):,.0f}"


def _signed_money(usd):
    return ("+" if usd >= 0 else "-") + _money(usd)


def _local(now_ms):
    return datetime.fromtimestamp(now_ms / 1000, ZoneInfo(BRIEF_TIMEZONE))


def format_brief(data, config, history, now_ms, *, late=False):
    res = data["results"]
    local = _local(now_ms)
    head = f"Liquidity briefing — {local.strftime('%a')} {local.day} {local.strftime('%b, %H:%M')} {TZ_LABEL}"
    lines = [head + (" (late)" if late else ""), ""]

    lines.append("MACRO")
    net = res.get("Fed net liquidity (FRED)")
    if net:
        lines.append(f"Fed net liquidity  {_money(net['net_b'] * 1e9)}  "
                     f"({_signed_money(net['week_change_b'] * 1e9)} w/w)")
    bits = []
    dxy, tnx, vix = res.get("Dollar index (Yahoo)"), res.get("US 10y (Yahoo)"), res.get("VIX (Yahoo)")
    if dxy:
        bits.append(f"Dollar index {dxy['price']:.1f} ({_signed((dxy['price'] / dxy['prev'] - 1) * 100, '.1f')}%)")
    if tnx:
        bits.append(f"10y {tnx['price']:.2f}% ({_signed((tnx['price'] - tnx['prev']) * 100, '.0f')}bp)")
    if vix:
        bits.append(f"VIX {vix['price']:.1f} ({_signed(vix['price'] - vix['prev'], '.1f')})")
    if bits:
        lines.append("  ·  ".join(bits))

    lines += ["", "CRYPTO MONEY"]
    stable = res.get("Stablecoins (DefiLlama)")
    if stable:
        lines.append(f"Stablecoins (USDT+USDC) {_money(stable['total_usd'])}  "
                     f"({_signed_money(stable['week_change_usd'])} w/w)")
    etf = res.get("BTC ETF flow (SoSoValue)")
    if etf:
        lines.append(f"BTC ETF flow ({etf['date']})  {_signed_money(etf['net_inflow_usd'])}")

    lines += ["", f"{'HYPERLIQUID':<7}  {'funding/h':>9}  {'OI 24h':>8}  {'±1% depth':>10}  {'spread':>7}"]
    markets = res.get("Hyperliquid markets")
    for coin in config.coins:
        if not markets:
            break
        m = markets[coin]
        change = history.change_pct(coin, now_ms, 24 * 3600)
        oi_text = _signed(change, ".1f") + "%" if change is not None else "warming up"
        depth = res.get(f"Hyperliquid depth {coin}")
        depth_text = spread_text = "n/a"
        if depth:
            depth_text = (">" if depth["truncated"] else "") + _money(depth["depth_usd"])
            spread_text = f"{depth['spread_bp']:.1f}bp"
        lines.append(f"{coin:<7}  {_signed(m['funding'] * 100, '.4f') + '%':>9}  {oi_text:>8}  "
                     f"{depth_text:>10}  {spread_text:>7}")

    lines += ["", "NEXT 24H EVENTS"]
    events = res.get("Economic calendar (ForexFactory)")
    if events is not None:
        now = datetime.fromtimestamp(now_ms / 1000, ZoneInfo(BRIEF_TIMEZONE))
        upcoming = [(s, t) for s, t in events if now <= s <= now + timedelta(hours=24)]
        if upcoming:
            for start, title in upcoming:
                lines.append(f"{start.astimezone(ZoneInfo(BRIEF_TIMEZONE)).strftime('%H:%M')}  USD  {title} (high)")
        else:
            lines.append("No high-impact USD events.")

    lines += ["", "Unavailable: " + (", ".join(data["unavailable"]) if data["unavailable"] else "none"),
              "Conditions, not signals."]
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def evaluate_alerts(data, config, history, now_ms):
    """[(key, text)] for every alert condition that is true right now."""
    res = data["results"]
    alerts = []
    markets = res.get("Hyperliquid markets") or {}
    for coin in config.coins:
        m = markets.get(coin)
        if not m:
            continue
        funding_pct = m["funding"] * 100
        if abs(funding_pct) >= config.funding_alert_pct:
            alerts.append((f"funding:{coin}",
                           f"{coin} hourly funding {_signed(funding_pct, '.4f')}% "
                           f"(about {_signed(funding_pct * 24 * 365, '.0f')}% a year): positioning is crowded one way."))
        change = history.change_pct(coin, now_ms, 3600)
        if change is not None and change <= -config.oi_drop_pct:
            alerts.append((f"flush:{coin}",
                           f"{coin} open interest fell {abs(change):.1f}% in the last hour: forced closing just happened."))
    now = _local(now_ms)
    for start, title in res.get("Economic calendar (ForexFactory)") or []:
        minutes = (start - now).total_seconds() / 60
        if 0 <= minutes <= config.event_minutes:
            alerts.append((f"event:{start.isoformat()}:{title}",
                           f"{title} (USD, high impact) at "
                           f"{start.astimezone(ZoneInfo(BRIEF_TIMEZONE)).strftime('%H:%M')} {TZ_LABEL}, "
                           f"in {minutes:.0f} min: the book usually thins before a release."))
    return [(key, f"LIQUIDITY ALERT — {text}\nConditions, not signals.") for key, text in alerts]


# ---------------------------------------------------------------------------
# Scheduler — runs on its own daemon thread, apart from the account loop
# ---------------------------------------------------------------------------

class BriefScheduler:
    """Sends the briefing at each configured slot, runs the alert checks and
    the open-interest sampling every 15 minutes, and answers /liquidity.

    State lives in DATA_DIR: which slot was last sent (so a restart neither
    double-sends nor resends an old slot) and when each alert condition last
    fired (so it repeats at most once every 4 hours). A slot missed by up to
    30 minutes is sent marked "(late)"; after that it is skipped."""

    def __init__(self, config, data_dir, send, *, fetch=collect,
                 clock=lambda: int(time.time() * 1000), sleep=time.sleep, spawn=None):
        self._config = config
        self._send = send
        self._fetch = fetch
        self._clock = clock
        self._sleep = sleep
        self._state_path = Path(data_dir) / STATE_FILE
        self._history = OiHistory(Path(data_dir) / OI_FILE)
        self._state = _read_json(self._state_path, {})
        if not isinstance(self._state, dict):
            self._state = {}
        self._state.setdefault("slots", {})
        self._state.setdefault("alerts", {})
        self._last_sample_ms = None
        self._cache = None  # (built_ms, text)
        self._lock = threading.Lock()
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True,
                                                             name="liquidity-reply").start())
        self._building = False
        self._building_lock = threading.Lock()

    def _save(self):
        _write_json(self._state_path, self._state)

    def _gather(self, now_ms):
        """Fetch everything once, record the OI sample. Caller holds the lock."""
        data = self._fetch(self._config)
        markets = data["results"].get("Hyperliquid markets")
        if markets:
            self._history.add(now_ms, {c: m["oi"] for c, m in markets.items()})
        self._last_sample_ms = now_ms
        return data

    def _due_slot(self, now_ms):
        local = _local(now_ms)
        for slot in self._config.times:
            hh, mm = map(int, slot.split(":"))
            slot_dt = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            lag_s = (local - slot_dt).total_seconds()
            if lag_s >= 0 and self._state["slots"].get(slot) != slot_dt.date().isoformat():
                return slot, slot_dt.date().isoformat(), lag_s
        return None

    def tick(self, now_ms=None):
        now_ms = self._clock() if now_ms is None else now_ms
        with self._lock:
            due = self._due_slot(now_ms)
            while due and due[2] > LATE_LIMIT_S:
                logger.info("briefing slot %s skipped: %d min late", due[0], due[2] // 60)
                self._state["slots"][due[0]] = due[1]
                self._save()
                due = self._due_slot(now_ms)
            sample_due = (self._last_sample_ms is None
                          or now_ms - self._last_sample_ms >= SAMPLE_INTERVAL_S * 1000)
            if not due and not sample_due:
                return
            data = self._gather(now_ms)
            if due:
                slot, day, lag_s = due
                text = format_brief(data, self._config, self._history, now_ms, late=lag_s > LATE_GRACE_S)
                self._send(text)
                self._state["slots"][slot] = day
                self._cache = (now_ms, text)
            self._send_alerts(data, now_ms)
            self._save()

    def _send_alerts(self, data, now_ms):
        alerts = self._state["alerts"]
        for key in [k for k, t in alerts.items() if now_ms - t > 24 * 3600 * 1000]:
            del alerts[key]
        for key, text in evaluate_alerts(data, self._config, self._history, now_ms):
            last = alerts.get(key)
            if last is not None and now_ms - last < ALERT_REPEAT_S * 1000:
                continue
            self._send(text)
            alerts[key] = now_ms

    def liquidity_reply(self, now_ms=None):
        """Text for /liquidity. Never blocks the Telegram command listener (it
        also answers /check): returns a briefing at most 5 minutes old, else
        an acknowledgement while a fresh briefing is built on its own thread
        and sent through `send`. At most one build runs at a time."""
        now_ms = self._clock() if now_ms is None else now_ms
        cached = self._cache
        if cached and 0 <= now_ms - cached[0] <= CACHE_MAX_AGE_S * 1000:
            return cached[1]
        with self._building_lock:
            start = not self._building
            self._building = True
        if start:
            try:
                self._spawn(self._build_and_send)
            except Exception:
                with self._building_lock:
                    self._building = False
                raise
        return LIQUIDITY_PENDING_TEXT

    def _build_and_send(self):
        try:
            with self._lock:
                now_ms = self._clock()
                data = self._gather(now_ms)
                text = format_brief(data, self._config, self._history, now_ms)
                self._cache = (now_ms, text)
                # _gather reset the sampling timer, so tick() will not look at
                # this data: run the alerts here or /liquidity would mute them.
                self._send_alerts(data, now_ms)
                self._save()
            self._send(text)
        except Exception:
            logger.exception("/liquidity build failed")
            self._send("Could not build the liquidity briefing — see the watcher logs.")
        finally:
            with self._building_lock:
                self._building = False

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("market brief tick failed — will retry")
            self._sleep(TICK_INTERVAL_S)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True, name="market-brief").start()


def main(argv=None):
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description="Print a live liquidity briefing (read-only).")
    parser.add_argument("--print", action="store_true", dest="do_print")
    args = parser.parse_args(argv)
    if not args.do_print:
        parser.error("use --print")
    config = BriefConfig.from_env()
    now_ms = int(time.time() * 1000)
    with tempfile.TemporaryDirectory() as tmp:
        history = OiHistory(Path(tmp) / OI_FILE)
        print(format_brief(collect(config), config, history, now_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
