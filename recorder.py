"""Standalone WebSocket flow recorder — a complete tape, not a sparse poll.

Why this exists: fetch_market.py's taker_delta cache is built from repeated
`recentTrades` REST polls, capped at ~10 trades/call (see fetch_market.py
bucket_taker_delta / merge_trade_cache). That is fine for the live /scalp
loop's cheap-and-cheerful read, but it is structurally incapable of full flow
research — the cache can hold well under 1% of real trades even when its
timestamps span the whole window (that's the `span_pct` vs `sample_pct` split
in fetch_market.py). This recorder instead holds a WebSocket connection open
and writes every message Hyperliquid sends for `trades`, `l2Book`, and `bbo` —
an append-only tape suitable for shadow/flow research, with the achieved
completeness ({--verify}) checked the same honest way: recorded trade count
per window vs Hyperliquid's own candle `n` (trade count) for that window.

Dependency: requires the `websockets` package (pure-Python asyncio WebSocket
client). Not stdlib. Verified present in this environment (websockets 16.0);
install with `pip install websockets` if missing. See
PLAN-flow-recorder-2026-07-12.md for the dependency note.

Output layout: `.flow_data/<COIN>_<channel>_<YYYY-MM-DD>.jsonl` (UTC day,
gitignored). One append-only file per coin per channel per UTC day, rotated
at UTC midnight based on local receive time (never exchange time — this repo
has a timezone-bug history; receive time is always available and monotonic,
exchange time is not, so rotation must not depend on it). When a day rolls,
the just-finished file is gzipped in place (`<...>.jsonl.gz`) and the plain
file removed only after the `.gz` is fully written and flushed — today's file
always stays plain. Readers (verify_sample_pct and any future one) read both
forms transparently. Nothing is ever deleted outright.

Every data record carries BOTH `exchange_ts_utc`/`exchange_ts_ms` (server
timestamp, when present) and `recv_ts_utc`/`recv_ts_ms` (local wall-clock at
receipt) — both explicitly UTC via iso_utc_ms(), which structurally prevents
the +8h local-timezone bug the same way fetch_market.iso_utc() does.

On reconnect (or at cold start after a prior session), a GAP record is
written into every open (coin, channel) file instead of silently splicing —
downstream analysis must see the hole, not a fabricated continuous tape.

CLI usage:
  python3 recorder.py [--coins HYPE,BTC] [--out-dir .flow_data]
                       [--status-interval-min 5]
  python3 recorder.py --verify [--coins HYPE] [--out-dir .flow_data]
                       [--window-min 30]
"""

import argparse
import asyncio
import glob
import gzip
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone

WS_URL = "wss://api.hyperliquid.xyz/ws"
CHANNELS = ("trades", "l2Book", "bbo")
DEFAULT_COINS = ["HYPE"]
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flow_data")
PING_INTERVAL_S = 30
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0
WATCHDOG_S = 30
DISK_FLOOR_GB = 10


class _StopRequested(Exception):
    """Internal signal: SIGINT/SIGTERM fired — unwind the recv loop cleanly."""


class _WatchdogTimeout(Exception):
    """Internal signal: no message on any stream for watchdog_s seconds. The
    socket may be half-open (this happens after Mac sleep or a network
    change: ping_interval=None means we never notice a missing pong), so we
    treat silence itself as a reason to force a reconnect through the normal
    path, which writes GAP records into every open file."""


def iso_utc_ms(ms):
    """Format an epoch-ms timestamp as an ISO-8601 UTC string with millisecond
    precision. Always UTC regardless of machine timezone — structurally
    prevents the +8h label bug the same way fetch_market.iso_utc() does, at
    the finer resolution flow research needs."""
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def day_str_utc(ms):
    """UTC calendar day (YYYY-MM-DD) for an epoch-ms timestamp."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def now_ms():
    return int(time.time() * 1000)


def file_path(out_dir, coin, channel, day_str):
    safe_coin = coin.replace(":", "_")
    return os.path.join(out_dir, f"{safe_coin}_{channel}_{day_str}.jsonl")


# ── pure record builders (no I/O — unit-testable without a socket) ─────────

def make_data_record(coin, channel, payload, exchange_ts_ms, recv_ms):
    """A normal data record. `payload` is the raw exchange object for this
    channel (trade dict, l2Book snapshot, or bbo snapshot) — stored verbatim
    so no field (notably `users` [buyer, seller] on trades) is lost."""
    return {
        "record_type": channel,
        "coin": coin,
        "exchange_ts_ms": exchange_ts_ms,
        "exchange_ts_utc": iso_utc_ms(exchange_ts_ms) if exchange_ts_ms is not None else None,
        "recv_ts_ms": recv_ms,
        "recv_ts_utc": iso_utc_ms(recv_ms),
        "data": payload,
    }


def make_gap_record(coin, channel, last_seen_exchange_ts_ms, reconnect_ms):
    """Emitted on (re)connect so downstream analysis sees a hole, not a
    silently-spanned gap. `last_seen_exchange_ts_ms` is None on cold start
    (no prior message this session)."""
    return {
        "record_type": "gap",
        "coin": coin,
        "channel": channel,
        "last_seen_exchange_ts_ms": last_seen_exchange_ts_ms,
        "last_seen_exchange_ts_utc": (
            iso_utc_ms(last_seen_exchange_ts_ms) if last_seen_exchange_ts_ms is not None else None),
        "reconnect_ts_ms": reconnect_ms,
        "reconnect_ts_utc": iso_utc_ms(reconnect_ms),
    }


def dedupe_trade(seen_tids, tid):
    """True if `tid` is new (should be written) and records it as seen.
    Session-scoped only (an in-memory set) — matches spec: dedupe within a
    session, not a persistent cross-restart cache like fetch_market's."""
    if tid in seen_tids:
        return False
    seen_tids.add(tid)
    return True


def _gzip_file_atomic(path):
    """Compress `path` to `path + '.gz'` via a temp file, fsync it, then
    rename into place and remove the plain file — only after the `.gz` is
    fully written and flushed to disk, so a crash mid-compress can never
    leave a truncated `.gz` or lose the plain file.

    If the day is already archived (the wall clock stepped back across UTC
    midnight and the day was reopened), the old archive's bytes are kept and
    the new rows are added as a second gzip member — gzip readers decode
    concatenated members as one stream, so nothing is ever overwritten.

    A member always ends with a newline (a crash fragment is terminated), so
    a later member can never glue its first record onto a partial line. If a
    crash hit between committing the archive and removing the plain file, the
    archive already ends with exactly this file's bytes; that case is
    detected and the plain file is just removed, not appended twice."""
    gz_path = path + ".gz"
    tmp_path = gz_path + ".tmp"
    if _drop_if_already_archived(path):
        return
    member_tail = b"\n" if _ends_mid_line(path) else b""
    with open(tmp_path, "wb") as raw:
        if os.path.exists(gz_path):
            with open(gz_path, "rb") as old:
                shutil.copyfileobj(old, raw)
        with open(path, "rb") as src, gzip.GzipFile(fileobj=raw, mode="wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.write(member_tail)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp_path, gz_path)
    os.remove(path)


def _drop_if_already_archived(path):
    """Remove `path` and return True if an interrupted rotation already
    committed exactly its bytes to `path + '.gz'`. Checked before a plain
    file is compressed AND before it is reopened for append — otherwise a
    leftover reopened as "today" (clock stepped back) would gain new rows,
    stop matching the archive, and be appended to it a second time."""
    gz_path = path + ".gz"
    if not (os.path.exists(path) and os.path.exists(gz_path)):
        return False
    member_tail = b"\n" if _ends_mid_line(path) else b""
    if not _gz_ends_with(gz_path, path, member_tail):
        return False
    os.remove(path)
    return True


def _gz_ends_with(gz_path, path, member_tail):
    """True if the decompressed archive ends with `path`'s bytes (+ the
    newline a fragment would have been given) — i.e. this plain file was
    already committed by an interrupted rotation. Only runs when both files
    exist, which is rare, so reading the archive through is acceptable.
    Records carry millisecond receive stamps, so an unrelated file matching
    the archive's tail byte-for-byte doesn't happen in practice."""
    with open(path, "rb") as f:
        want = f.read() + member_tail
    if not want:
        return True
    tail = b""
    with gzip.open(gz_path, "rb") as g:
        for chunk in iter(lambda: g.read(1 << 20), b""):
            tail = (tail + chunk)[-len(want):]
    return tail == want


def _ends_mid_line(path):
    """True if `path` is non-empty and its last byte isn't a newline — a
    crash mid-write left a partial record there."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return False
        f.seek(-1, os.SEEK_END)
        return f.read(1) != b"\n"


def _day_from_jsonl_path(path):
    """Extract the `YYYY-MM-DD` day from a `<coin>_<channel>_<day>.jsonl`
    path. Splits from the right so coins containing `_` (from `:`, see
    file_path) don't confuse the parse — channel and day are fixed-shape."""
    name = os.path.basename(path)
    if not name.endswith(".jsonl"):
        return None
    return name[:-len(".jsonl")].rsplit("_", 1)[-1]


def iter_jsonl_records(path):
    """Yield decoded JSON records for one day: first `path + '.gz'` (a
    rotated, finished day), then the plain `path` (today's file). Both can
    exist briefly if the clock stepped back into an archived day, so both are
    read. Shared by verify_sample_pct and any future reader; malformed lines
    are skipped."""
    for opener, mode, target in ((gzip.open, "rt", path + ".gz"), (open, "r", path)):
        if not os.path.exists(target):
            continue
        with opener(target, mode) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ── append-only per (coin, channel, day) file handles with rotation ────────

class FlowFileSet:
    """Owns the open file handles for every (coin, channel) pair, rotating to
    a new file when the UTC day (of the record being written) changes. Each
    write is flushed immediately — this is a tape, not a buffer; a crash must
    not lose the last few seconds. On rotation (and at startup, for files
    left plain by a crash) the finished day is gzipped in place; today's file
    always stays plain so nothing races an open file. Never deletes a file
    outright — a plain file becomes a `.gz`, nothing more."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self._handles = {}   # (coin, channel) -> (day_str, file obj)
        self._compress_stale_startup_files()

    def _compress_stale_startup_files(self):
        today = day_str_utc(now_ms())
        for path in glob.glob(os.path.join(self.out_dir, "*.jsonl")):
            day = _day_from_jsonl_path(path)
            if day is not None and day != today:
                _gzip_file_atomic(path)

    def write(self, coin, channel, record, day_str):
        key = (coin, channel)
        cur = self._handles.get(key)
        if cur is None or cur[0] != day_str:
            if cur is not None:
                old_path = cur[1].name
                cur[1].close()
                _gzip_file_atomic(old_path)
            path = file_path(self.out_dir, coin, channel, day_str)
            _drop_if_already_archived(path)
            partial_tail = os.path.exists(path) and _ends_mid_line(path)
            f = open(path, "a")
            if partial_tail:
                # Terminate the crash fragment so the next record (usually the
                # restart GAP) lands on its own line instead of being glued
                # onto a malformed one and skipped by readers.
                f.write("\n")
            self._handles[key] = (day_str, f)
        f = self._handles[key][1]
        f.write(json.dumps(record) + "\n")
        f.flush()

    def close_all(self):
        for _, f in self._handles.values():
            f.close()
        self._handles = {}


# ── status / liveness ───────────────────────────────────────────────────

def status_path_for(out_dir):
    return os.path.join(out_dir, "status.json")


def build_status(counts, last_recv_ms, connected, now=None,
                  last_recv_per_stream=None, reconnects_24h=0,
                  gap_seconds_24h=0.0, disk_low=False, disk_free_gb=None):
    now = now_ms() if now is None else now
    lag_s = None
    if last_recv_ms is not None:
        lag_s = round((now - last_recv_ms) / 1000, 1)
    lag_s_per_stream = {
        key: round((now - ms) / 1000, 1)
        for key, ms in (last_recv_per_stream or {}).items()
    }
    return {
        "ts_utc": iso_utc_ms(now),
        "connected": connected,
        "lag_s": lag_s,
        "lag_s_per_stream": lag_s_per_stream,
        "counts": dict(counts),
        "reconnects_24h": reconnects_24h,
        "gap_seconds_24h": round(gap_seconds_24h, 1),
        "disk_low": disk_low,
        "disk_free_gb": round(disk_free_gb, 1) if disk_free_gb is not None else None,
    }


def write_status(out_dir, status):
    path = status_path_for(out_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, path)


# ── recorder ─────────────────────────────────────────────────────────────

class FlowRecorder:
    """Holds one WS connection open across `coins` x CHANNELS, writing every
    message to FlowFileSet and tracking per-(coin,channel) liveness for GAP
    records and the status line. `clock` is injectable for tests."""

    def __init__(self, coins, out_dir=DEFAULT_OUT_DIR, status_interval_s=300,
                clock=now_ms, watchdog_s=WATCHDOG_S, disk_floor_gb=DISK_FLOOR_GB):
        self.coins = list(coins)
        self.out_dir = out_dir
        self.status_interval_s = status_interval_s
        self.clock = clock
        self.watchdog_s = watchdog_s
        self.disk_floor_gb = disk_floor_gb
        self.files = FlowFileSet(out_dir)
        self._seen_tids = {c: set() for c in self.coins}
        # Per-coin UTC day the dedupe set above was last cleared for — cleared
        # again whenever a trade's recv day rolls past it, so the set is
        # bounded to roughly one day's tids rather than growing forever over
        # a months-long session. HL replays ~30s of trades on subscribe /
        # reconnect, so a duplicate landing exactly at this boundary is
        # possible either way; downstream readers must dedupe by tid across
        # files themselves, not rely on this set's lifetime.
        self._trade_dedupe_day = {c: None for c in self.coins}
        self._last_exchange_ts = {}   # (coin, channel) -> ms
        self._counts = {}             # (coin, channel) -> int
        self._last_recv_ms = None
        self._last_recv_per_stream = {}  # "COIN:channel" -> ms
        self._reconnect_ts = []       # ms timestamps of reconnects (not cold start)
        self._gap_events = []         # (start_ms, end_ms) — one per closed outage
        # Local recv time the current outage began (last message before the
        # drop); None while connected. Failed retries inside one outage leave
        # it untouched, so an outage is counted once however many retries it
        # takes.
        self._down_since_ms = None
        self._last_outage_end_ms = None
        self._connected = False
        self._stop = None             # set in run() (needs a live loop)

    # -- message handling (pure-ish: no socket I/O, easy to unit test) --

    def handle_trade(self, coin, trade):
        """One trade object from the `trades` channel data array. Dedupes by
        `tid` within the current UTC day (see _trade_dedupe_day); writes only
        new trades. `users` [buyer, seller] is preserved verbatim inside
        `data` — load-bearing for wallet-flow research."""
        recv_ms = self.clock()
        day = day_str_utc(recv_ms)
        if self._trade_dedupe_day.get(coin) != day:
            self._seen_tids[coin] = set()
            self._trade_dedupe_day[coin] = day
        tid = trade.get("tid")
        if tid is not None and not dedupe_trade(self._seen_tids[coin], tid):
            return
        ex_ms = trade.get("time")
        rec = make_data_record(coin, "trades", trade, ex_ms, recv_ms)
        self._write(coin, "trades", rec, ex_ms, recv_ms)

    def handle_snapshot(self, channel, coin, payload):
        """One l2Book or bbo snapshot. No dedupe — every snapshot is written
        (the point is a complete tape, not a compressed one)."""
        recv_ms = self.clock()
        ex_ms = payload.get("time") if isinstance(payload, dict) else None
        rec = make_data_record(coin, channel, payload, ex_ms, recv_ms)
        self._write(coin, channel, rec, ex_ms, recv_ms)

    def _write(self, coin, channel, record, exchange_ts_ms, recv_ms):
        day = day_str_utc(recv_ms)
        self.files.write(coin, channel, record, day)
        if exchange_ts_ms is not None:
            self._last_exchange_ts[(coin, channel)] = exchange_ts_ms
        # string key ("COIN:channel") — self._counts feeds build_status(),
        # which gets json.dump'd to the status file; tuple keys aren't
        # JSON-serializable.
        key = f"{coin}:{channel}"
        self._counts[key] = self._counts.get(key, 0) + 1
        self._last_recv_ms = recv_ms
        self._last_recv_per_stream[key] = recv_ms

    def emit_gaps(self, reconnect_ms=None):
        """Write a GAP record into every (coin, channel) file — called on
        reconnect (and once at cold start, where last_seen is None for every
        pair, documenting "recording started here")."""
        reconnect_ms = self.clock() if reconnect_ms is None else reconnect_ms
        for coin in self.coins:
            for channel in CHANNELS:
                last_seen = self._last_exchange_ts.get((coin, channel))
                rec = make_gap_record(coin, channel, last_seen, reconnect_ms)
                day = day_str_utc(reconnect_ms)
                self.files.write(coin, channel, rec, day)

    def mark_disconnected(self):
        """A live connection just dropped: open an outage starting at the last
        message received. No-op if an outage is already open, so failed
        retries during a long outage don't add reconnects or gap time."""
        if self._down_since_ms is None:
            # Start from the later of the last message and the last outage's
            # end: a reconnect that drops again before any data arrives must
            # not reopen time already counted.
            marks = [t for t in (self._last_recv_ms, self._last_outage_end_ms) if t is not None]
            self._down_since_ms = max(marks) if marks else self.clock()

    def end_outage(self, end_ms, reconnected=True):
        """Close the open outage (if any): write GAP records into every file,
        count its wall-clock length once toward `gap_seconds_24h`, and count
        one reconnect unless it ended because the recorder is stopping."""
        if self._down_since_ms is None:
            return
        self.emit_gaps(reconnect_ms=end_ms)
        self._gap_events.append((self._down_since_ms, end_ms))
        if reconnected:
            self._reconnect_ts.append(end_ms)
        self._down_since_ms = None
        self._last_outage_end_ms = end_ms

    def _prune_old_events(self, now_ms_):
        # Keep a 48h buffer (double the 24h window status reports) so a
        # status check right at the edge of the window is never short.
        cutoff = now_ms_ - 2 * 24 * 3600 * 1000
        self._reconnect_ts = [t for t in self._reconnect_ts if t >= cutoff]
        self._gap_events = [(s, e) for s, e in self._gap_events if e >= cutoff]

    def _disk_free_gb(self):
        try:
            return shutil.disk_usage(self.out_dir).free / (1024 ** 3)
        except OSError:
            return None

    def status(self):
        now = self.clock()
        self._prune_old_events(now)
        cutoff_24h = now - 24 * 3600 * 1000
        reconnects_24h = sum(1 for t in self._reconnect_ts if t >= cutoff_24h)
        # Only the part of each outage inside the window counts, and an
        # outage still in progress counts up to now — so a long outage can't
        # exceed the window and a current one doesn't read as zero.
        outages = list(self._gap_events)
        if self._down_since_ms is not None:
            outages.append((self._down_since_ms, now))
        gap_seconds_24h = sum(max(0, min(e, now) - max(s, cutoff_24h))
                              for s, e in outages) / 1000
        free_gb = self._disk_free_gb()
        disk_low = free_gb is not None and free_gb < self.disk_floor_gb
        if disk_low:
            print(f"{iso_utc_ms(now)} recorder: disk low — {free_gb:.1f} GB free "
                  f"under {self.out_dir} (floor {self.disk_floor_gb} GB); "
                  "never deleting, alert only", file=sys.stderr)
        return build_status(
            self._counts, self._last_recv_ms, self._connected, now=now,
            last_recv_per_stream=self._last_recv_per_stream,
            reconnects_24h=reconnects_24h, gap_seconds_24h=gap_seconds_24h,
            disk_low=disk_low, disk_free_gb=free_gb)

    def dispatch_message(self, msg):
        """Route one decoded WS message ({"channel": ..., "data": ...}) to the
        right handler. Unknown / control channels (subscriptionResponse,
        pong, error) are ignored — they're not tape data."""
        channel = msg.get("channel")
        data = msg.get("data")
        if channel == "trades" and isinstance(data, list):
            for trade in data:
                coin = trade.get("coin")
                if coin in self._seen_tids:
                    self.handle_trade(coin, trade)
        elif channel in ("l2Book", "bbo") and isinstance(data, dict):
            coin = data.get("coin")
            if coin in self.coins:
                self.handle_snapshot(channel, coin, data)
        # subscriptionResponse / pong / error / anything else: not tape data.

    # -- live networking (needs `websockets`; imported lazily so the pure
    #    helpers above stay importable/testable without the dependency) --

    def _subscribe_messages(self):
        for coin in self.coins:
            for channel in CHANNELS:
                yield {"method": "subscribe",
                       "subscription": {"type": channel, "coin": coin}}

    async def run(self):
        try:
            import websockets
        except ImportError:
            raise SystemExit(
                "recorder.py requires the 'websockets' package "
                "(pip install websockets) — see PLAN-flow-recorder-2026-07-12.md")

        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass  # signals unsupported on this platform (e.g. some Windows setups)

        self.emit_gaps()  # cold-start marker: "recording begins here"
        status_task = asyncio.create_task(self._status_loop())
        backoff = RECONNECT_BASE_S
        try:
            while not self._stop.is_set():
                try:
                    async with websockets.connect(WS_URL, ping_interval=None) as ws:
                        self._connected = True
                        self.end_outage(self.clock())
                        backoff = RECONNECT_BASE_S
                        for sub in self._subscribe_messages():
                            await ws.send(json.dumps(sub))
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                        try:
                            await self._recv_loop(ws)
                        finally:
                            ping_task.cancel()
                except _StopRequested:
                    break
                except _WatchdogTimeout as exc:
                    print(f"{iso_utc_ms(self.clock())} recorder: {exc} — "
                          "reconnecting", file=sys.stderr)
                except Exception as exc:
                    print(f"{iso_utc_ms(self.clock())} recorder: connection error "
                          f"({exc!r}) — reconnecting", file=sys.stderr)
                if self._connected:
                    self.mark_disconnected()
                self._connected = False
                if self._stop.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break  # stop set during backoff wait
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, RECONNECT_MAX_S)
        finally:
            status_task.cancel()
            # Stopping mid-outage: close it so the tape records the hole up to
            # shutdown (not a reconnect). A clean stop also leaves the final
            # status saying disconnected — the break on _StopRequested skips
            # the reset inside the loop.
            self.end_outage(self.clock(), reconnected=False)
            self._connected = False
            self.files.close_all()
            write_status(self.out_dir, self.status())

    async def _recv_loop(self, ws):
        while not self._stop.is_set():
            recv_wait = asyncio.create_task(ws.recv())
            stop_wait = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                {recv_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED,
                timeout=self.watchdog_s)
            for p in pending:
                p.cancel()
            if stop_wait in done:
                raise _StopRequested()
            if recv_wait not in done:
                # Watchdog: no message on any subscribed stream within
                # watchdog_s. A half-open socket after Mac sleep (or a
                # network change) never surfaces as an error — ws.recv()
                # just blocks forever, since ping_interval=None and this
                # recorder's ping loop never checks for a pong — so silence
                # itself is the only signal we get. Let the caller (run())
                # close the connection and reconnect through the normal
                # path, which writes GAP records.
                raise _WatchdogTimeout(
                    f"no message on any stream for {self.watchdog_s}s")
            raw = recv_wait.result()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            self.dispatch_message(msg)

    async def _ping_loop(self, ws):
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                return

    async def _status_loop(self):
        while True:
            write_status(self.out_dir, self.status())
            await asyncio.sleep(self.status_interval_s)


# ── --verify: recorded completeness vs candle `n` from REST ────────────────

def verify_sample_pct(out_dir, coin, window_min=30, now=None):
    """Read the recorder's trades file(s) for `coin`, count trades in the
    trailing `window_min` window, and compare against Hyperliquid's own
    candle `n` (trade count) for the same window via REST — the same honest
    completeness metric as fetch_market.bucket_taker_delta's sample_pct,
    applied to the recorder's own output as its acceptance test.
    """
    import fetch_market as fm

    now = now_ms() if now is None else now
    win_ms = window_min * 60 * 1000
    win_start = now - win_ms

    trade_ts = []
    seen_tids = set()
    DAY = 86_400_000
    days = [day_str_utc(d) for d in range((win_start // DAY) * DAY, now + 1, DAY)]
    for day in days:
        path = file_path(out_dir, coin, "trades", day)
        for rec in iter_jsonl_records(path):
            if rec.get("record_type") != "trades":
                continue
            trade = rec.get("data") or {}
            ex_ms = rec.get("exchange_ts_ms")
            if ex_ms is None or ex_ms < win_start or ex_ms > now:
                continue
            tid = trade.get("tid")
            if tid is not None:
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)
            trade_ts.append(ex_ms)

    if not trade_ts:
        return {"coin": coin, "window_min": window_min, "recorded_trades": 0,
                "candle_n_sum": None, "sample_pct": None,
                "note": "no recorded trades in window"}

    # Partial edge minutes (connected/stopped mid-minute) would understate
    # completeness — compare only over whole minutes fully inside both the
    # window and the recorded span.
    MIN_MS = 60_000
    span_start = ((max(win_start, min(trade_ts)) + MIN_MS - 1) // MIN_MS) * MIN_MS
    span_end = (min(now, max(trade_ts)) // MIN_MS) * MIN_MS
    if span_end <= span_start:
        return {"coin": coin, "window_min": window_min, "recorded_trades": len(trade_ts),
                "candle_n_sum": None, "sample_pct": None,
                "note": "recorded span shorter than one whole minute"}

    recorded = sum(1 for t in trade_ts if span_start <= t < span_end)
    candles = fm.fetch_candles(coin, "1m", span_start, span_end)
    in_span = {c["t_ms"]: c.get("n") for c in candles if span_start <= c["t_ms"] < span_end}
    minutes = (span_end - span_start) // MIN_MS
    if len(in_span) != minutes or any(n is None for n in in_span.values()):
        # A missing candle or count would drop that minute from the
        # denominator while its recorded trades stay in the numerator,
        # overstating completeness. Unknown is reported as unknown.
        return {"coin": coin, "window_min": window_min, "recorded_trades": recorded,
                "candle_n_sum": None, "sample_pct": None,
                "note": f"exchange trade counts missing for "
                        f"{minutes - sum(n is not None for n in in_span.values())} "
                        f"of {minutes} minutes — completeness unknown"}
    n_sum = sum(in_span.values())
    sample_pct = round(min(100.0, 100.0 * recorded / n_sum), 1) if n_sum > 0 else None
    return {
        "coin": coin,
        "window_min": window_min,
        "compared_span_min": round((span_end - span_start) / MIN_MS, 1),
        "recorded_trades": recorded,
        "candle_n_sum": n_sum,
        "sample_pct": sample_pct,
    }


# ── CLI ──────────────────────────────────────────────────────────────────

def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--coins", default=",".join(DEFAULT_COINS),
                    help="comma-separated coin list (default HYPE)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--status-interval-min", type=float, default=5.0)
    ap.add_argument("--watchdog-s", type=float, default=WATCHDOG_S,
                    help="reconnect if no message on any stream for this many "
                         "seconds (default %(default)s)")
    ap.add_argument("--disk-floor-gb", type=float, default=DISK_FLOOR_GB,
                    help="warn (never delete) when free disk under --out-dir "
                         "falls below this many GB (default %(default)s)")
    ap.add_argument("--verify", action="store_true",
                    help="compare recorded trade count vs candle n from REST and exit")
    ap.add_argument("--window-min", type=int, default=30,
                    help="--verify: trailing window to check (minutes)")
    args = ap.parse_args(argv[1:])
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]

    if args.verify:
        for coin in coins:
            result = verify_sample_pct(args.out_dir, coin, args.window_min)
            print(json.dumps(result, indent=2))
        return 0

    recorder = FlowRecorder(coins, out_dir=args.out_dir,
                            status_interval_s=args.status_interval_min * 60,
                            watchdog_s=args.watchdog_s,
                            disk_floor_gb=args.disk_floor_gb)
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
