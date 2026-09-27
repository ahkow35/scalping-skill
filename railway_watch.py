"""Always-on Railway watcher for the read-only Hyperliquid account monitor.

This process never places, cancels or closes an order, and adds no exchange
read beyond account_monitor.py's own existing calls (READ_TYPES in
account_api.py is unchanged). It calls `account_monitor.check()` directly in
a loop — never the `watch` subcommand, and never anything that parses printed
text — derives the set of currently active problems from the check report's
structured fields, and alerts over Telegram when a problem appears, repeats
that alert every 30 minutes while it lasts, and sends an ALL CLEAR when it
ends. It also serves the latest report over a small, token-protected HTTP
endpoint for `/scalp` (a later PR) to read, and pings an optional dead-man
health check on every loop pass that produced a report.

Report classification (`classify`) is the one place this module decides how
much to trust a report: only a fully evaluated report (the normal
account_risk.evaluate() success path) may clear a previously active problem
or fire a new underwater-add alert. A failed report (DATA_UNAVAILABLE, an
accounting mismatch, or a latched HALT re-reported from a failed check)
carries no positions and must never be read as "problem gone" — so it leaves
every other problem exactly as it was and only feeds the monitor-failure
streak. A config/unsupported report (missing config, an unsupported account
mode) is its own problem and, being a definite read rather than a failed one,
resets the failure streak without touching position-based problems either.

Run: `python3 railway_watch.py`. See ACCOUNT-MONITOR.md for the environment
variables this reads on boot.
"""

import hmac
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import account_monitor
from account_observation import underwater_add_reason


logger = logging.getLogger("railway_watch")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TIMEOUT_S = 5.0
HEALTHCHECK_TIMEOUT_S = 5.0
NORMAL_INTERVAL_S = 30
FAST_INTERVAL_S = 15
REPEAT_INTERVAL_MS = 30 * 60 * 1000
FAILURE_THRESHOLD_MS = 5 * 60 * 1000
ALIVE_HOUR, ALIVE_MINUTE = 9, 0
OUTBOX_RETRY_S = 60
OUTBOX_MAX_PENDING = 200
DEFAULT_TIMEZONE = "Asia/Singapore"


# ---------------------------------------------------------------------------
# Report classification and problem derivation — structured fields only,
# never the status word (which shows only the most serious problem at a
# time) and never printed/parsed text.
# ---------------------------------------------------------------------------

def classify(report):
    """'evaluated' (a trustworthy positions/latch reading), 'config' (a
    definite but unsupported-mode/missing-config reading), or 'failed' (no
    reliable reading — DATA_UNAVAILABLE, an accounting mismatch, or a latched
    HALT re-reported from a failed check, none of which carry positions)."""
    daily = report.get("daily") or {}
    if daily.get("net_pnl_usdc") is not None:
        return "evaluated"
    if report.get("status") in ("UNSUPPORTED", "CONFIG_REQUIRED"):
        return "config"
    return "failed"


def stop_problems(report):
    """Per-coin unprotected/partial stop coverage, keyed by coin."""
    problems = {}
    for stop in (report.get("observation") or {}).get("stops", []):
        if stop["fully_covered"]:
            continue
        severity = "unprotected" if stop["covered_size"] <= 0 else "partial"
        problems[f"STOP:{stop['coin']}"] = (
            f"{stop['coin']} {stop['side']} position stop coverage is {severity} "
            f"({stop['covered_size']:g}/{stop['position_size']:g})")
    return problems


def oversized_problems(report):
    """Per-coin oversized-position warnings, keyed by coin."""
    return {
        f"OVERSIZED:{item['coin']}": (
            f"{item['coin']} position notional {item['notional_usdc']:,.2f} USDC exceeds "
            f"the size cap {item['cap_usdc']:,.2f} USDC")
        for item in report.get("oversized_positions", [])
    }


def account_problems(report):
    """Singleton problems read directly from structured fields, not the
    status word: the daily loss latch and open trigger-distance risk can
    coexist with (and be hidden behind) a more serious status."""
    problems = {}
    if report.get("daily_breach_latched"):
        problems["HALT"] = "daily account-loss limit breached; trading halted for the risk day"
    open_risk = report.get("open_trigger_distance_risk_usdc")
    remaining = report.get("remaining_daily_budget_usdc")
    if open_risk is not None and remaining is not None and open_risk > remaining:
        problems["EXCESS_OPEN_RISK"] = (
            "open stop-loss trigger-distance risk exceeds the remaining daily budget")
    return problems


def underwater_events(report):
    """One-shot events keyed by (coin, fill_time_ms) — each add alerts once."""
    return {(item["coin"], item["fill_time_ms"]): underwater_add_reason(item)
            for item in report.get("underwater_adds", [])}


class Watcher:
    """Tracks which problems are currently active and decides what to send.

    No network call happens here: `step` is pure given a report and a clock,
    so every alerting rule — appear, repeat, clear, the failure streak, the
    once-per-day underwater alert — is directly testable with fake reports.
    """

    def __init__(self, *, timezone=DEFAULT_TIMEZONE, repeat_interval_ms=REPEAT_INTERVAL_MS,
                 failure_threshold_ms=FAILURE_THRESHOLD_MS,
                 alive_hour=ALIVE_HOUR, alive_minute=ALIVE_MINUTE):
        self.tz = ZoneInfo(timezone)
        self.repeat_interval_ms = repeat_interval_ms
        self.failure_threshold_ms = failure_threshold_ms
        self.alive_hour, self.alive_minute = alive_hour, alive_minute
        self.active = {}          # problem_id -> {"message": str, "last_alerted_ms": int}
        self.seen_underwater = set()
        self.first_failure_ms = None
        self.last_alive_date = None

    def boot(self, now_ms):
        """Call once at startup. Marks today as already summarized so a
        mid-day deploy does not immediately fire the daily alive message —
        that first fires tomorrow at the configured local time."""
        self.last_alive_date = self._local(now_ms).date().isoformat()

    def _local(self, now_ms):
        return datetime.fromtimestamp(now_ms / 1000, self.tz)

    def _note(self, pid, message, now_ms, messages):
        existing = self.active.get(pid)
        if existing is None:
            self.active[pid] = {"message": message, "last_alerted_ms": now_ms}
            messages.append(f"PROBLEM: {message}")
            return
        existing["message"] = message
        if now_ms - existing["last_alerted_ms"] >= self.repeat_interval_ms:
            existing["last_alerted_ms"] = now_ms
            messages.append(f"PROBLEM (ongoing): {message}")

    def _clear(self, pid, now_ms, messages):
        existing = self.active.pop(pid, None)
        if existing is not None:
            messages.append(f"ALL CLEAR: {existing['message']}")

    def _sync_group(self, prefix, current, now_ms, messages):
        for pid in [pid for pid in self.active if pid.startswith(prefix) and pid not in current]:
            self._clear(pid, now_ms, messages)
        for pid, message in current.items():
            self._note(pid, message, now_ms, messages)

    def step(self, report, now_ms):
        """Feed one account_monitor.check() report. Returns messages to send,
        in the order they should be sent."""
        messages = []
        kind = classify(report)

        if kind == "failed":
            if self.first_failure_ms is None:
                self.first_failure_ms = now_ms
            if now_ms - self.first_failure_ms >= self.failure_threshold_ms:
                self._note("MONITOR_FAILURE",
                           "account monitor has not produced a reading for over 5 minutes",
                           now_ms, messages)
        else:
            self.first_failure_ms = None
            self._clear("MONITOR_FAILURE", now_ms, messages)

        if kind == "config":
            reasons = report.get("reasons") or []
            detail = reasons[0] if reasons else str(report.get("status"))
            self._note("CONFIG_UNSUPPORTED", f"account monitor: {detail}", now_ms, messages)
        elif kind == "evaluated":
            self._clear("CONFIG_UNSUPPORTED", now_ms, messages)

        if kind == "evaluated":
            self._sync_group("STOP:", stop_problems(report), now_ms, messages)
            self._sync_group("OVERSIZED:", oversized_problems(report), now_ms, messages)
            current = account_problems(report)
            for pid in ("HALT", "EXCESS_OPEN_RISK"):
                if pid in current:
                    self._note(pid, current[pid], now_ms, messages)
                else:
                    self._clear(pid, now_ms, messages)
            for key, message in underwater_events(report).items():
                if key not in self.seen_underwater:
                    self.seen_underwater.add(key)
                    messages.append(f"PROBLEM: {message}")
        # kind == "failed": every other problem is left exactly as it was — a
        # failed report has no positions and must never read as "gone".

        return messages

    def alive_summary(self, report, now_ms):
        """A short daily summary at the configured local time, once a day."""
        local = self._local(now_ms)
        today = local.date().isoformat()
        if today == self.last_alive_date or (local.hour, local.minute) < (self.alive_hour, self.alive_minute):
            return []
        self.last_alive_date = today
        observation = report.get("observation") or {}
        equity = observation.get("equity_usdc")
        parts = [f"watcher alive — status {report.get('status', 'UNKNOWN')}"]
        if equity is not None:
            parts.append(f"equity {equity:,.2f} USDC")
        if self.active:
            parts.append(f"{len(self.active)} problem(s) active")
        return [" | ".join(parts)]

    def in_midnight_window(self, now_ms):
        """23:58–00:02 local time, so the watcher's own day baseline lands
        as a near-reset observation rather than a partial-day one."""
        local = self._local(now_ms)
        minutes = local.hour * 60 + local.minute
        return minutes >= 23 * 60 + 58 or minutes < 2


# ---------------------------------------------------------------------------
# Telegram and the dead-man ping — both run off the check loop's thread, so
# a slow or failing send never blocks or slows a check; failures are logged
# without the token or full URL.
# ---------------------------------------------------------------------------

class Outbox:
    """Queue of messages delivered in order by a background thread.

    `put` never blocks. A message that fails to deliver stays at the head of
    the queue and is retried every `retry_s` seconds — so a failed ALL CLEAR
    or one-shot underwater alert is not lost. If the queue passes
    `max_pending` during a long outage, the oldest message is dropped (and
    logged) so memory stays bounded."""

    def __init__(self, deliver, *, retry_s=OUTBOX_RETRY_S, max_pending=OUTBOX_MAX_PENDING):
        self._deliver = deliver
        self._retry_s = retry_s
        self._max_pending = max_pending
        self._pending = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def put(self, message):
        with self._lock:
            self._pending.append(message)
            if len(self._pending) > self._max_pending:
                self._pending.popleft()
                logger.warning("outbox full; dropped the oldest unsent message")
        self._wake.set()

    def pending(self):
        with self._lock:
            return list(self._pending)

    def drain_once(self):
        """Deliver queued messages in order until one fails or the queue is
        empty. Returns True if the queue was emptied."""
        while True:
            with self._lock:
                if not self._pending:
                    return True
                message = self._pending[0]
            try:
                delivered = self._deliver(message)
            except Exception:
                logger.exception("outbox delivery raised")
                delivered = False
            if not delivered:
                return False
            with self._lock:
                if self._pending and self._pending[0] is message:
                    self._pending.popleft()

    def run_forever(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            if not self.drain_once():
                self._wake.wait(self._retry_s)
                self._wake.set()

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True, name="outbox").start()


class BackgroundPinger:
    """Fires the dead-man ping on a background thread, at most one in flight,
    so a slow health-check host never delays a check."""

    def __init__(self, ping):
        self._ping = ping
        self._busy = threading.Lock()

    def __call__(self):
        if not self._busy.acquire(blocking=False):
            return  # previous ping still running; the next pass pings again
        def work():
            try:
                self._ping()
            except Exception:
                logger.exception("healthcheck ping failed")
            finally:
                self._busy.release()
        threading.Thread(target=work, daemon=True, name="ping").start()


def send_telegram(token, chat_id, text, *, timeout=TELEGRAM_TIMEOUT_S, post=requests.post):
    try:
        response = post(TELEGRAM_URL.format(token=token), json={"chat_id": chat_id, "text": text},
                         timeout=timeout)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("telegram send failed: %s%s", type(exc).__name__,
                        f" (HTTP {status})" if status else "")
        return False


def ping_healthcheck(url, *, timeout=HEALTHCHECK_TIMEOUT_S, get=requests.get):
    if not url:
        return
    try:
        get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("healthcheck ping failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Latest-report HTTP address — stdlib only, token-protected, nothing else
# served. Built as a handler factory so it can be exercised in tests without
# binding a real socket (see tests/test_railway_watch.py).
# ---------------------------------------------------------------------------

class ReportState:
    """The latest produced report, pre-serialized on the loop thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._body = None

    def update(self, produced_at_ms, report):
        body = json.dumps({"produced_at_ms": produced_at_ms, "report": report},
                           allow_nan=False).encode("utf-8")
        with self._lock:
            self._body = body

    def snapshot(self):
        with self._lock:
            return self._body


def make_handler(state, token):
    """Build a BaseHTTPRequestHandler bound to this state and token. GET
    /report needs 'Authorization: Bearer <token>', compared with
    hmac.compare_digest; a missing/wrong token is a bare 401, an unset token
    is a bare 503 (never open). GET /health is unauthenticated and serves no
    data. Nothing else is served."""

    class ReportHandler(BaseHTTPRequestHandler):
        server_version = "railway-watch/1"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

        def _respond(self, code, body=b"", content_type="text/plain"):
            self.send_response(code)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._respond(200, b"ok")
                return
            if self.path != "/report":
                self._respond(404)
                return
            if not token:
                self._respond(503)
                return
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            expected = f"Bearer {token}".encode("utf-8")
            if not hmac.compare_digest(supplied, expected):
                self._respond(401)
                return
            body = state.snapshot()
            if body is None:
                self._respond(503)
                return
            self._respond(200, body, content_type="application/json")

    return ReportHandler


# ---------------------------------------------------------------------------
# Config on boot
# ---------------------------------------------------------------------------

def resolve_data_dir(env=os.environ):
    raw = env.get("DATA_DIR")
    return Path(raw) if raw else account_monitor.DEFAULT_DIR


def configure_from_env(data_dir, env=os.environ):
    """Run the monitor's existing configure step from env. Missing required
    env is a clear startup error; this raises SystemExit (non-zero)."""
    wallet = env.get("MONITOR_WALLET")
    daily_loss = env.get("MONITOR_DAILY_LOSS_USDC")
    missing = [name for name, value in
               (("MONITOR_WALLET", wallet), ("MONITOR_DAILY_LOSS_USDC", daily_loss)) if not value]
    if missing:
        raise SystemExit(f"railway_watch startup: missing required env var(s): {', '.join(missing)}")
    args = ["--data-dir", str(data_dir), "configure", "--wallet", wallet,
            "--daily-loss-usdc", daily_loss,
            "--timezone", env.get("MONITOR_TIMEZONE", DEFAULT_TIMEZONE)]
    cap = env.get("MONITOR_MAX_POSITION_NOTIONAL_USDC")
    if cap:
        args += ["--max-position-notional-usdc", cap]
    code = account_monitor.main(args)
    if code != 0:
        raise SystemExit(f"railway_watch startup: account_monitor configure failed (exit {code})")


# ---------------------------------------------------------------------------
# The loop — injectable so tests run it with a fake check function, a fake
# clock and a fake sender, with no network and no real sleeping.
# ---------------------------------------------------------------------------

def loop(watcher, *, data_dir, check, send, ping, on_report=None,
          clock=lambda: int(time.time() * 1000), sleep=time.sleep, iterations=None):
    now_ms = clock()
    watcher.boot(now_ms)
    _safe_send(send, "watcher started")
    count = 0
    while iterations is None or count < iterations:
        now_ms = clock()
        try:
            report = check(data_dir)
            produced = True
        except Exception:
            logger.exception("unexpected error calling account_monitor.check()")
            report = {"status": "DATA_UNAVAILABLE", "reasons": ["unexpected watcher error"]}
            produced = False
        if on_report is not None:
            try:
                on_report(now_ms, report)
            except Exception:
                logger.exception("could not publish the latest report")
        for message in watcher.step(report, now_ms):
            _safe_send(send, message)
        for message in watcher.alive_summary(report, now_ms):
            _safe_send(send, message)
        if produced:
            try:
                ping()
            except Exception:
                logger.exception("healthcheck ping failed")
        count += 1
        if iterations is not None and count >= iterations:
            break
        interval = FAST_INTERVAL_S if watcher.in_midnight_window(clock()) else NORMAL_INTERVAL_S
        elapsed_s = (clock() - now_ms) / 1000
        sleep(max(0.0, interval - elapsed_s))


def _safe_send(send, message):
    try:
        send(message)
    except Exception:
        logger.exception("send failed")


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = resolve_data_dir()
    configure_from_env(data_dir)

    timezone_name = os.environ.get("MONITOR_TIMEZONE", DEFAULT_TIMEZONE)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    healthcheck_url = os.environ.get("HEALTHCHECK_PING_URL")
    report_token = os.environ.get("REPORT_TOKEN")
    port = int(os.environ.get("PORT", "8080"))

    def deliver(text):
        if bot_token and chat_id:
            return send_telegram(bot_token, chat_id, text)
        logger.warning("Telegram not configured; message suppressed: %s", text)
        return True

    outbox = Outbox(deliver)
    outbox.start()

    state = ReportState()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state, report_token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("report server listening on :%d (token %s)", port,
                "configured" if report_token else "UNSET — /report will 503")

    watcher = Watcher(timezone=timezone_name)
    try:
        loop(watcher, data_dir=data_dir, check=account_monitor.check, send=outbox.put,
             ping=BackgroundPinger(lambda: ping_healthcheck(healthcheck_url)),
             on_report=state.update)
    finally:
        server.shutdown()


def main():
    try:
        run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
