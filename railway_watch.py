"""Always-on Railway watcher for the read-only Hyperliquid account monitor.

This process never places, cancels or closes an order. Account reads are only
account_monitor.py's own existing calls (READ_TYPES in account_api.py is
unchanged). The market briefing (market_brief.py, its own daemon thread) adds
PUBLIC market reads only — Hyperliquid `metaAndAssetCtxs` and `l2Book`, plus
free keyless web sources — and never reads the account or touches an order.
It calls `account_monitor.check()` directly in
a loop — never the `watch` subcommand, and never anything that parses printed
text — derives the set of currently active problems from the check report's
structured fields, and alerts over Telegram when a problem appears, repeats
that alert every 30 minutes while it lasts, and sends an ALL CLEAR when it
ends. It also serves the latest report over a small, token-protected HTTP
endpoint for `/scalp` (a later PR) to read, and pings an optional dead-man
health check on every loop pass that produced a report. It also answers a
`/check` command sent from the configured Telegram chat with that latest
report, judged by the same entry gate `/scalp` applies to it, and a
`/liquidity` command with the latest market-liquidity briefing.

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
import market_brief
from account_observation import underwater_add_reason


logger = logging.getLogger("railway_watch")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TIMEOUT_S = 5.0
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
TELEGRAM_LONG_POLL_S = 25
# (connect, read): the read allowance covers Telegram holding the long poll.
TELEGRAM_UPDATES_TIMEOUT = (5.0, TELEGRAM_LONG_POLL_S + 10.0)
COMMAND_ERROR_BACKOFF_S = 5
COMMAND_CONFLICT_BACKOFF_S = 60
# /check replies share the bot and chat with alerts; Telegram rate-limits a
# chat, so a burst of /check must never push an alert into a 429 retry.
CHECK_MIN_INTERVAL_S = 10
TELEGRAM_MAX_TEXT = 4000
HEALTHCHECK_TIMEOUT_S = 5.0
NORMAL_INTERVAL_S = 30
FAST_INTERVAL_S = 15
REPEAT_INTERVAL_MS = 30 * 60 * 1000
FAILURE_THRESHOLD_MS = 5 * 60 * 1000
ALIVE_HOUR, ALIVE_MINUTE = 9, 0
OUTBOX_RETRY_S = 60
OUTBOX_MAX_PENDING = 200
DEFAULT_TIMEZONE = "Asia/Singapore"

# Optional recorder check (PR C) — unset RECORDER_STATUS_URL means this
# feature is off with zero behaviour change; see run() and loop().
RECORDER_STATUS_TIMEOUT_S = 5.0
RECORDER_POLL_INTERVAL_S = 60
RECORDER_SILENT_THRESHOLD_S = 5 * 60
RECORDER_UPLOAD_FAILING_THRESHOLD_S = 24 * 3600


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
        self.recorder_first_silent_ms = None  # PR C: recorder-silent sustain timer

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
            if kind == "evaluated":
                # Only a usable reading ends a monitor failure; a config or
                # unsupported read still leaves the account unmonitored.
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
            risk_known = (report.get("open_trigger_distance_risk_usdc") is not None
                          and report.get("remaining_daily_budget_usdc") is not None)
            for pid in ("HALT", "EXCESS_OPEN_RISK"):
                if pid in current:
                    self._note(pid, current[pid], now_ms, messages)
                elif pid == "EXCESS_OPEN_RISK" and not risk_known:
                    # Unknown risk (e.g. another position lost its stop) is
                    # not evidence the excess ended; keep any active alert.
                    continue
                else:
                    self._clear(pid, now_ms, messages)
            for key, message in underwater_events(report).items():
                if key not in self.seen_underwater:
                    self.seen_underwater.add(key)
                    messages.append(f"PROBLEM: {message}")
        # kind == "failed": every other problem is left exactly as it was — a
        # failed report has no positions and must never read as "gone".

        return messages

    def recorder_step(self, status, now_ms):
        """Feed one poll of the recorder's `/status` (PR C), or None if it
        was unreachable/unparseable. Two problem ids, through the same
        appear/30-min-repeat/all-clear machinery as account problems
        (`_note`/`_clear`) and the same Outbox — but a fully separate group
        (`RECORDER_*` ids) that never reads or touches account-derived
        problems, `/report`, or entry_allowed. A recorder-check exception is
        caught by the caller (`loop`), never here.

        RECORDER_SILENT: the status endpoint was unreachable, or reported
        not connected, or no message for 5 minutes — sustained for 5 minutes
        before alerting, so one bad poll doesn't flap. Clears only when a
        poll comes back healthy (reachable, connected, recent message).

        RECORDER_UPLOAD_FAILING: uploads disabled by missing S3
        configuration (an immediate, definite condition — like
        CONFIG_UNSUPPORTED above, not something that needs sustaining), or a
        finished day still unconfirmed more than 24h after that UTC day
        ended, or uploads failing for over 24h — the latter two computed
        server-side by the recorder itself (see railway_record.py's
        /status) so no date math happens here. Left exactly as it was on an
        unreachable poll (`status is None`) — no evidence either way, same
        rule `step()` applies to account problems on a failed report."""
        messages = []
        healthy = (status is not None
                   and status.get("connected") is True
                   and isinstance(status.get("seconds_since_last_message"), (int, float))
                   and status["seconds_since_last_message"] < RECORDER_SILENT_THRESHOLD_S)
        if healthy:
            self.recorder_first_silent_ms = None
            self._clear("RECORDER_SILENT", now_ms, messages)
        else:
            if self.recorder_first_silent_ms is None:
                self.recorder_first_silent_ms = now_ms
            if now_ms - self.recorder_first_silent_ms >= RECORDER_SILENT_THRESHOLD_S * 1000:
                self._note("RECORDER_SILENT",
                           "recorder status is unreachable, disconnected, or silent for over 5 minutes",
                           now_ms, messages)

        if status is not None:
            disabled = status.get("uploads_enabled") is False
            day_stale = status.get("seconds_since_oldest_unconfirmed_day_ended")
            failing_s = status.get("seconds_upload_failing")
            failing = (
                disabled
                or (isinstance(day_stale, (int, float)) and day_stale > RECORDER_UPLOAD_FAILING_THRESHOLD_S)
                or (isinstance(failing_s, (int, float)) and failing_s > RECORDER_UPLOAD_FAILING_THRESHOLD_S)
            )
            if failing:
                reason = ("recorder uploads disabled — missing or invalid S3 bucket configuration" if disabled else
                           "recorder upload is more than 24h behind")
                self._note("RECORDER_UPLOAD_FAILING", reason, now_ms, messages)
            else:
                self._clear("RECORDER_UPLOAD_FAILING", now_ms, messages)

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
        # Recorder problems are counted apart so they never read as account problems.
        recorder = sum(1 for pid in self.active if pid.startswith("RECORDER_"))
        account = len(self.active) - recorder
        if account:
            parts.append(f"{account} account problem(s) active")
        if recorder:
            parts.append(f"{recorder} recorder problem(s) active")
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


def _spawn_daemon(target):
    threading.Thread(target=target, daemon=True, name="recorder-poll").start()


class RecorderPoller:
    """Polls the recorder's `/status` on a background thread, at most one in
    flight, so the recorder can never stall the account loop — a per-read
    timeout alone does not bound a body that trickles in slowly. The loop
    calls `tick` every pass; it starts a poll when one is due and hands back
    any finished result. A poll still running after `stuck_after_s` counts
    as unreachable (None), at most once per interval, until it returns — and
    its answer, when it finally lands, is read as unreachable too: a reply
    that took that long describes a moment long gone and must never clear
    an alert raised in the meantime."""

    def __init__(self, url, fetch, *, interval_s, stuck_after_s, spawn=_spawn_daemon):
        self._url = url
        self._fetch = fetch
        self._interval_ms = interval_s * 1000
        self._stuck_ms = stuck_after_s * 1000
        self._spawn = spawn
        self._lock = threading.Lock()
        self._in_flight_since_ms = None
        self._last_start_ms = None
        self._last_stuck_ms = None
        self._has_result = False
        self._result = None
        self._result_started_ms = None

    def tick(self, now_ms):
        """The statuses (zero or one) to feed Watcher.recorder_step now."""
        with self._lock:
            start = self._in_flight_since_ms is None and (
                self._last_start_ms is None or now_ms - self._last_start_ms >= self._interval_ms)
            if start:
                self._in_flight_since_ms = self._last_start_ms = now_ms
        if start:
            try:
                self._spawn(self._work)
            except Exception:
                logger.exception("could not start the recorder poll")
                with self._lock:
                    self._in_flight_since_ms = None
        with self._lock:
            if self._has_result:
                self._has_result = False
                if now_ms - self._result_started_ms >= self._stuck_ms:
                    return [None]
                return [self._result]
            since = self._in_flight_since_ms
            if since is not None and now_ms - since >= self._stuck_ms and (
                    self._last_stuck_ms is None or now_ms - self._last_stuck_ms >= self._interval_ms):
                self._last_stuck_ms = now_ms
                return [None]
        return []

    def _work(self):
        try:
            status = self._fetch(self._url)
        except Exception:
            logger.exception("recorder status poll raised")
            status = None
        with self._lock:
            self._result, self._has_result = status, True
            self._result_started_ms = self._in_flight_since_ms
            self._in_flight_since_ms = self._last_stuck_ms = None


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


def fetch_recorder_status(url, *, timeout=RECORDER_STATUS_TIMEOUT_S, get=requests.get):
    """GET the recorder's `/status` (PR C). Returns the parsed JSON dict, or
    None on any failure (timeout, connection error, non-200, non-JSON body,
    or a body that isn't a dict) — Watcher.recorder_step treats None the
    same as "unreachable". The short timeout is what keeps this from ever
    slowing the account loop."""
    try:
        response = get(url, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    return body if isinstance(body, dict) else None


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
        self._latest = None

    def update(self, produced_at_ms, report):
        body = json.dumps({"produced_at_ms": produced_at_ms, "report": report},
                           allow_nan=False).encode("utf-8")
        with self._lock:
            self._body = body
            # A private copy of exactly what /report serves, for /check.
            self._latest = (produced_at_ms, json.loads(body)["report"])

    def snapshot(self):
        with self._lock:
            return self._body

    def latest(self):
        """(produced_at_ms, report) as last served on /report, or None."""
        with self._lock:
            return self._latest


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

# ---------------------------------------------------------------------------
# Telegram /check — answers only the configured chat, from the latest report
# the loop produced. Never runs a check of its own and never touches the
# alert state, so it cannot slow or change the account loop.
# ---------------------------------------------------------------------------

def _usd(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:,.2f}"


def _signed_usd(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"{'-' if value < 0 else '+'}${abs(value):,.2f}"


def format_check(latest, now_ms, timezone_name=DEFAULT_TIMEZONE):
    """Plain-text reply to /check. ALLOWED appears only when /scalp's
    remote_check would also let entry through for this report at this
    moment: entry_allowed true and account_monitor.watcher_report_problem
    finding nothing (a complete CLEAR, fresh within its 60 s limit)."""
    if latest is None:
        return "No account reading yet — the watcher has just started. Try /check again in a minute."
    produced_at_ms, report = latest
    age_s = (now_ms - produced_at_ms) / 1000
    problem = account_monitor.watcher_report_problem(report, age_s)
    allowed = report.get("entry_allowed") is True and problem is None
    when = datetime.fromtimestamp(produced_at_ms / 1000, ZoneInfo(timezone_name)).strftime("%H:%M:%S")
    lines = [f"ACCOUNT CHECK — reading taken {when} ({max(age_s, 0):.0f} s ago)",
             "ENTRY: ALLOWED — the account has room; /scalp still runs its own checks" if allowed
             else f"ENTRY: BLOCKED ({report.get('status') or 'UNKNOWN'})"]
    if classify(report) == "evaluated":
        daily = report.get("daily") or {}
        observation = report.get("observation") or {}
        lines.append(f"Daily budget left: {_usd(report.get('remaining_daily_budget_usdc'))} of "
                     f"{_usd(daily.get('limit_usdc'))} (today's P&L {_signed_usd(daily.get('net_pnl_usdc'))})")
        lines.append(f"Open stop risk: {_usd(report.get('open_trigger_distance_risk_usdc'))}")
        lines.append(f"Equity: {_usd(observation.get('equity_usdc'))}")
        for stop in observation.get("stops", []):
            coverage = "fully covered" if stop.get("fully_covered") else "NOT fully covered"
            lines.append(f"{stop.get('coin')} {stop.get('side')}: stops {coverage} "
                         f"({stop.get('covered_size', 0):g}/{stop.get('position_size', 0):g})")
        lines.extend(oversized_problems(report).values())
    notes = ([problem] if problem else []) + [r for r in report.get("reasons") or [] if isinstance(r, str)]
    if notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in notes)
    lines.append("Read-only. ALLOWED means room in the budget, not a trade signal.")
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_MAX_TEXT else text[:TELEGRAM_MAX_TEXT - 1] + "…"


def _command_name(text):
    """The lowercase command in '/check', '/check@SomeBot' or '/liquidity now'."""
    if not isinstance(text, str) or not text.strip():
        return None
    return text.split()[0].split("@", 1)[0].lower()


def _is_check_command(text):
    """'/check' or '/check@SomeBot', any case, optionally followed by words."""
    return _command_name(text) == "/check"


class TelegramCommands:
    """Long-polls the bot's getUpdates and answers /check (and /liquidity,
    when a liquidity_answer is given) from the
    configured chat only; every other chat and message is ignored. The
    getUpdates URL carries the bot token, so failures log only the exception
    class and HTTP status — never the exception text, which names the URL.
    A /check sent while the watcher was down is dropped at startup rather
    than answered late with a reading from a later moment. At most one reply
    per CHECK_MIN_INTERVAL_S across both commands; extra messages inside that window are
    dropped so they cannot crowd out alerts on the same chat."""

    def __init__(self, token, chat_id, answer, *, liquidity_answer=None, get=requests.get,
                  send=send_telegram, sleep=time.sleep, clock=time.monotonic):
        self._token = token
        self._chat_id = str(chat_id).strip()
        self._answers = {"/check": answer}
        if liquidity_answer is not None:
            self._answers["/liquidity"] = liquidity_answer
        self._get = get
        self._send = send
        self._sleep = sleep
        self._offset = None
        self._backlog_skipped = False
        self._clock = clock
        self._last_reply_s = None

    def _updates(self, params, timeout):
        response = self._get(TELEGRAM_UPDATES_URL.format(token=self._token), params=params,
                              timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not (isinstance(body, dict) and body.get("ok") is True and isinstance(body.get("result"), list)):
            raise ValueError("malformed getUpdates body")
        return body["result"]

    def _advance(self, update):
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            self._offset = max(self._offset or 0, update_id + 1)

    def _skip_backlog(self):
        for update in self._updates({"offset": -1, "timeout": 0}, TELEGRAM_TIMEOUT_S):
            self._advance(update)
        self._backlog_skipped = True

    def _handle(self, update):
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self._chat_id:
            return
        command = _command_name(message.get("text"))
        answer = self._answers.get(command)
        if answer is None:
            return
        now_s = self._clock()
        if self._last_reply_s is not None and now_s - self._last_reply_s < CHECK_MIN_INTERVAL_S:
            logger.info("%s ignored: within %d s of the last reply", command, CHECK_MIN_INTERVAL_S)
            return
        self._last_reply_s = now_s
        try:
            text = answer()
        except Exception as exc:
            logger.warning("could not build the %s reply: %s", command, type(exc).__name__)
            text = f"Could not build the {command} reply — see the watcher logs."
        self._send(self._token, self._chat_id, text)

    def poll_once(self):
        if not self._backlog_skipped:
            self._skip_backlog()
        params = {"timeout": TELEGRAM_LONG_POLL_S, "allowed_updates": json.dumps(["message"])}
        if self._offset is not None:
            params["offset"] = self._offset
        for update in self._updates(params, TELEGRAM_UPDATES_TIMEOUT):
            # Advance first: a message that fails to handle is never re-read.
            self._advance(update)
            self._handle(update)

    def step(self):
        """One poll; any failure is logged without its text and backed off."""
        try:
            self.poll_once()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("telegram command poll failed: %s%s", type(exc).__name__,
                            f" (HTTP {status})" if status else "")
            # 409: another getUpdates consumer or a webhook holds the bot.
            self._sleep(COMMAND_CONFLICT_BACKOFF_S if status == 409 else COMMAND_ERROR_BACKOFF_S)

    def run_forever(self):
        while True:
            self.step()

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True, name="telegram-commands").start()


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
          clock=lambda: int(time.time() * 1000), sleep=time.sleep, iterations=None,
          recorder_status_url=None, fetch_recorder_status=fetch_recorder_status,
          recorder_poll_interval_s=RECORDER_POLL_INTERVAL_S, recorder_spawn=_spawn_daemon):
    now_ms = clock()
    watcher.boot(now_ms)
    _safe_send(send, "watcher started")
    count = 0
    recorder_poller = RecorderPoller(
        recorder_status_url, fetch_recorder_status, interval_s=recorder_poll_interval_s,
        stuck_after_s=2 * recorder_poll_interval_s, spawn=recorder_spawn,
    ) if recorder_status_url else None
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
        # Optional recorder check (PR C): unset recorder_status_url means
        # this whole block never runs — zero behaviour change. Polled at
        # most once a minute on a background thread (see RecorderPoller), and
        # any exception here is swallowed so it can never crash or stall the
        # account loop above. Never touches /report, entry_allowed, or any
        # account-derived problem.
        if recorder_poller is not None:
            try:
                # A fresh clock read: the account check above can take tens
                # of seconds, and an old now_ms would let a stale reply pass
                # the poller's age check.
                recorder_now_ms = clock()
                for status in recorder_poller.tick(recorder_now_ms):
                    for message in watcher.recorder_step(status, recorder_now_ms):
                        _safe_send(send, message)
            except Exception:
                logger.exception("recorder check raised — ignoring this pass")
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
    # PR C, optional: e.g. http://<recorder-service>.railway.internal:8080/status.
    # Unset (the default) means the recorder check never runs — see loop().
    recorder_status_url = os.environ.get("RECORDER_STATUS_URL")

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

    # Market briefing: its own daemon thread, so a slow or failing data
    # source can never delay the account loop. BRIEF_ENABLED=false turns it off.
    scheduler = None
    try:
        brief_config = market_brief.BriefConfig.from_env()
        if brief_config.enabled:
            scheduler = market_brief.BriefScheduler(brief_config, data_dir, outbox.put)
            scheduler.start()
            logger.info("market briefing started: %s SGT, coins %s",
                        ",".join(brief_config.times), ",".join(brief_config.coins))
    except Exception:
        logger.exception("market briefing could not start — account watcher continues without it")
        scheduler = None

    if bot_token and chat_id:
        TelegramCommands(bot_token, chat_id, lambda: format_check(
            state.latest(), int(time.time() * 1000), timezone_name),
            liquidity_answer=scheduler.liquidity_reply if scheduler else None).start()
        logger.info("telegram /check%s listener started", " and /liquidity" if scheduler else "")

    watcher = Watcher(timezone=timezone_name)
    try:
        loop(watcher, data_dir=data_dir, check=account_monitor.check, send=outbox.put,
             ping=BackgroundPinger(lambda: ping_healthcheck(healthcheck_url)),
             on_report=state.update, recorder_status_url=recorder_status_url)
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
