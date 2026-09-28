from datetime import datetime
import io
import json
import threading

import pytest
import requests

import railway_watch as rw
from test_account_observation import WALLET, snapshot, stop
from test_account_risk import CONFIG, MIDNIGHT, observe, start_day, underwater_long_fills


TZ = "Asia/Singapore"


def evaluated(**kwargs):
    report, _ = observe(snapshot(**kwargs))
    return report


def make_watcher():
    return rw.Watcher(timezone=TZ)


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def test_classify_a_real_clear_report_as_evaluated():
    _, state = start_day()
    assert rw.classify(evaluated(now=MIDNIGHT + 1000)) in ("evaluated",)


def test_classify_unsupported_mode_as_config():
    report, _ = observe(snapshot(now=MIDNIGHT, mode="portfolioMargin"))
    assert report["status"] == "UNSUPPORTED"
    assert rw.classify(report) == "config"


def test_classify_config_required_as_config(tmp_path):
    import account_monitor as monitor
    report = monitor.check(tmp_path / "does-not-exist", fetch=lambda *a: pytest.fail("must not fetch"))
    assert report["status"] == "CONFIG_REQUIRED"
    assert rw.classify(report) == "config"


def test_classify_data_unavailable_as_failed():
    from account_risk import failure_result
    report = failure_result("offline")
    assert report["status"] == "DATA_UNAVAILABLE"
    assert rw.classify(report) == "failed"


def test_classify_a_latched_halt_from_a_failed_check_as_failed_not_evaluated():
    # account_risk.failure_result reports status HALT (from persisted state)
    # even though this specific check produced no fresh positions.
    from account_risk import failure_result
    report = failure_result("offline", {"day": {"tripped": True}})
    assert report["status"] == "HALT"
    assert rw.classify(report) == "failed"


# ---------------------------------------------------------------------------
# Watcher.step: appear / repeat / clear
# ---------------------------------------------------------------------------

def test_unprotected_stop_appears_repeats_after_30_min_and_clears():
    w = make_watcher()
    t0 = MIDNIGHT
    _, state = observe(snapshot(now=t0 - 20_000))
    unprotected, state = observe(snapshot(now=t0, size=10), state)  # no stop orders
    assert unprotected["status"] == "UNPROTECTED"

    appear = w.step(unprotected, t0)
    assert any("PROBLEM:" in m and "HYPE" in m and "unprotected" in m for m in appear)

    too_soon = w.step(unprotected, t0 + 5 * 60_000)
    assert too_soon == []

    repeat = w.step(unprotected, t0 + 31 * 60_000)
    assert any("PROBLEM (ongoing)" in m for m in repeat)

    covered, _ = observe(snapshot(now=t0 + 1000, size=10, orders=[stop(trigger=95)]), state)
    assert covered["status"] == "CLEAR"
    cleared = w.step(covered, t0 + 32 * 60_000)
    assert any("ALL CLEAR" in m and "HYPE" in m for m in cleared)


def test_oversized_position_appears_and_clears():
    w = make_watcher()
    config = {**CONFIG, "max_position_notional_usdc": 500}
    report, _ = evaluate_with_config(config, snapshot(now=MIDNIGHT, size=10, orders=[stop(trigger=95)]))
    assert report["oversized_positions"]
    appear = w.step(report, MIDNIGHT)
    assert any("PROBLEM:" in m and "exceeds the size cap" in m for m in appear)

    small, _ = evaluate_with_config(config, snapshot(now=MIDNIGHT + 1000, size=1, orders=[stop(trigger=95, size=1)]))
    cleared = w.step(small, MIDNIGHT + 1000)
    assert any("ALL CLEAR" in m and "exceeds the size cap" in m for m in cleared)


def evaluate_with_config(config, data):
    from account_risk import evaluate
    return evaluate(data, config, None, now_ms=data["asof_ms"])


def test_halt_appears_from_daily_breach_latch():
    w = make_watcher()
    report, state = start_day()
    assert report["status"] == "CLEAR"
    halted, state = observe(snapshot(now=MIDNIGHT + 1000, equity=880, upnl=-120, size=10,
                                     orders=[stop(trigger=80)]), state)
    assert halted["status"] == "HALT"
    messages = w.step(halted, MIDNIGHT + 1000)
    assert any("PROBLEM:" in m and "daily account-loss limit" in m for m in messages)


def test_excess_open_risk_appears_independently_of_status_priority():
    w = make_watcher()
    _, state = start_day()
    # mark = 100 + upnl/size = 97; trigger 60 (above the liquidation price of
    # 50, so it still counts as coverage) -> distance 37 * size 10 = 370,
    # well over the 70 USDC remaining budget (limit 100 - 30 loss).
    report, _ = observe(snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10,
                                 orders=[stop(trigger=60)]), state)
    assert report["status"] == "EXCESS_OPEN_RISK"
    messages = w.step(report, MIDNIGHT + 1000)
    assert any("PROBLEM:" in m and "trigger-distance risk exceeds" in m for m in messages)


# ---------------------------------------------------------------------------
# A failed report must never clear a problem
# ---------------------------------------------------------------------------

def test_failed_report_never_produces_an_all_clear():
    from account_risk import failure_result
    w = make_watcher()
    _, state = observe(snapshot(now=MIDNIGHT - 20_000))
    unprotected, state = observe(snapshot(now=MIDNIGHT, size=10), state)
    w.step(unprotected, MIDNIGHT)
    assert "STOP:HYPE" in w.active

    failed = failure_result("exchange unavailable")
    assert failed["status"] == "DATA_UNAVAILABLE"
    messages = w.step(failed, MIDNIGHT + 30_000)
    assert not any("CLEAR" in m for m in messages)
    assert "STOP:HYPE" in w.active  # untouched, not silently dropped either


def test_accounting_mismatch_failure_carries_stale_observation_but_is_not_evaluated():
    # evaluate()'s accounting-mismatch path merges failure_result over a
    # report that still has `observation` with positions/stops — classify()
    # must not be fooled by that into treating it as a trustworthy read.
    from account_risk import evaluate
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, size=10, orders=[stop(trigger=95)])
    data["fills"] = [{"coin": "HYPE", "tid": 1, "time": MIDNIGHT + 500,
                      "closedPnl": "-999", "fee": "0", "feeToken": "USDC"}]
    report, _ = evaluate(data, CONFIG, state, now_ms=data["asof_ms"])
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report.get("observation") is not None  # the trap: positions are present
    assert rw.classify(report) == "failed"


# ---------------------------------------------------------------------------
# Monitor failure: 5-minute rule, including a single blip
# ---------------------------------------------------------------------------

def test_single_failure_blip_never_alerts():
    from account_risk import failure_result
    w = make_watcher()
    failed = failure_result("blip")
    messages = w.step(failed, MIDNIGHT)
    assert messages == []
    recovered, _ = start_day()
    messages = w.step(recovered, MIDNIGHT + 1000)
    assert not any("MONITOR_FAILURE" in m for m in messages)  # never appeared, nothing to clear


def test_monitor_failure_fires_only_after_five_minutes_of_consecutive_failures():
    from account_risk import failure_result
    w = make_watcher()
    failed = failure_result("offline")
    t0 = MIDNIGHT
    for minutes in (0, 1, 2, 3, 4):
        messages = w.step(failed, t0 + minutes * 60_000)
        assert not any("MONITOR_FAILURE" in m for m in messages), f"fired too early at +{minutes}min"
    messages = w.step(failed, t0 + 5 * 60_000)
    joined = " ".join(messages)
    assert "PROBLEM:" in joined and "not produced a reading for over 5 minutes" in joined


def test_monitor_failure_clears_on_next_success():
    from account_risk import failure_result
    w = make_watcher()
    failed = failure_result("offline")
    t0 = MIDNIGHT
    w.step(failed, t0)
    w.step(failed, t0 + 5 * 60_000)
    assert "MONITOR_FAILURE" in w.active
    recovered, _ = start_day()
    messages = w.step(recovered, t0 + 6 * 60_000)
    assert any("ALL CLEAR" in m and "reading" in m for m in messages)
    assert "MONITOR_FAILURE" not in w.active


def test_an_intervening_success_resets_the_failure_clock():
    from account_risk import failure_result
    w = make_watcher()
    failed = failure_result("offline")
    t0 = MIDNIGHT
    w.step(failed, t0)
    w.step(failed, t0 + 4 * 60_000)
    recovered, _ = start_day()
    w.step(recovered, t0 + 4 * 60_000 + 1000)
    messages = w.step(failed, t0 + 4 * 60_000 + 2000)
    assert not any("MONITOR_FAILURE" in m for m in messages)  # clock restarted, not yet 5 min


# ---------------------------------------------------------------------------
# config/unsupported problems
# ---------------------------------------------------------------------------

def test_unsupported_mode_raises_and_clears_config_problem():
    w = make_watcher()
    unsupported, _ = observe(snapshot(now=MIDNIGHT, mode="portfolioMargin"))
    assert unsupported["status"] == "UNSUPPORTED"
    messages = w.step(unsupported, MIDNIGHT)
    assert any("PROBLEM:" in m for m in messages)
    assert "CONFIG_UNSUPPORTED" in w.active

    recovered, _ = start_day()
    messages = w.step(recovered, MIDNIGHT + 1000)
    assert any("ALL CLEAR" in m for m in messages)
    assert "CONFIG_UNSUPPORTED" not in w.active


def test_config_problem_is_not_cleared_by_a_failed_read():
    from account_risk import failure_result
    w = make_watcher()
    unsupported, _ = observe(snapshot(now=MIDNIGHT, mode="portfolioMargin"))
    w.step(unsupported, MIDNIGHT)
    assert "CONFIG_UNSUPPORTED" in w.active
    failed = failure_result("offline")
    messages = w.step(failed, MIDNIGHT + 1000)
    assert not any("CLEAR" in m for m in messages)
    assert "CONFIG_UNSUPPORTED" in w.active


# ---------------------------------------------------------------------------
# Underwater add: a one-shot event, alerts once
# ---------------------------------------------------------------------------

def test_underwater_add_alerts_once_even_across_repeated_checks():
    w = make_watcher()
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10, orders=[stop(trigger=95)])
    data["fills"] = underwater_long_fills()
    report, _ = observe(data, state)
    assert report["underwater_adds"]

    first = w.step(report, MIDNIGHT + 1000)
    assert any("added to a losing long" in m for m in first)

    second = w.step(report, MIDNIGHT + 2000)
    assert not any("added to a losing" in m for m in second)

    third = w.step(report, MIDNIGHT + 40 * 60_000)  # even well past the repeat window
    assert not any("added to a losing" in m for m in third)


# ---------------------------------------------------------------------------
# Midnight fast-poll window
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("h,m,s,expected", [
    (23, 57, 59, False),
    (23, 58, 0, True),
    (23, 59, 59, True),
    (0, 0, 0, True),
    (0, 1, 59, True),
    (0, 2, 0, False),
    (12, 0, 0, False),
])
def test_midnight_window_boundaries(h, m, s, expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    w = make_watcher()
    tz = ZoneInfo(TZ)
    dt = datetime(2026, 9, 28, h, m, s, tzinfo=tz)
    now_ms = int(dt.timestamp() * 1000)
    assert w.in_midnight_window(now_ms) is expected


# ---------------------------------------------------------------------------
# Telegram: failed send never raises, never stops the loop
# ---------------------------------------------------------------------------

def test_send_telegram_logs_and_returns_false_on_failure_without_raising():
    import requests

    def broken_post(url, json, timeout):
        raise requests.ConnectionError("connection refused to https://api.telegram.org/botSECRET123/sendMessage")

    ok = rw.send_telegram("SECRET123", "chat", "hello", post=broken_post)
    assert ok is False


def test_telegram_failure_text_never_appears_in_logs(caplog):
    import logging
    import requests

    def broken_post(url, json, timeout):
        raise requests.ConnectionError("connection refused to https://api.telegram.org/botSECRET123/sendMessage")

    with caplog.at_level(logging.WARNING, logger="railway_watch"):
        rw.send_telegram("SECRET123", "chat", "hello", post=broken_post)
    assert "SECRET123" not in caplog.text
    assert "api.telegram.org" not in caplog.text


def test_healthcheck_ping_failure_is_logged_not_raised(caplog):
    import logging
    import requests

    def broken_get(url, timeout):
        raise requests.Timeout("timed out hitting https://hc-ping.com/deadbeef-secret-uuid")

    with caplog.at_level(logging.WARNING, logger="railway_watch"):
        rw.ping_healthcheck("https://hc-ping.com/deadbeef-secret-uuid", get=broken_get)
    assert "deadbeef-secret-uuid" not in caplog.text


def test_healthcheck_ping_is_a_noop_when_url_unset():
    calls = []
    rw.ping_healthcheck(None, get=lambda *a, **k: calls.append(1))
    assert calls == []


def test_loop_continues_after_a_send_raises():
    reports = iter([
        {"status": "DATA_UNAVAILABLE", "reasons": ["x"], "daily_breach_latched": False},
        {"status": "DATA_UNAVAILABLE", "reasons": ["x"], "daily_breach_latched": False},
    ])
    sent = []

    def flaky_send(message):
        sent.append(message)
        if len(sent) == 1:
            raise RuntimeError("simulated telegram outage")

    pings = []
    clocks = iter([MIDNIGHT + i * 30_000 for i in range(10)])
    w = make_watcher()
    rw.loop(w, data_dir="unused", check=lambda _dir: next(reports), send=flaky_send,
            ping=lambda: pings.append(1), clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=2)
    assert sent == ["watcher started"]  # boot message raised but loop kept going
    assert len(pings) == 2  # both failed checks still produced a report


# ---------------------------------------------------------------------------
# Config applied from env, on boot
# ---------------------------------------------------------------------------

def test_configure_from_env_writes_config_and_resolves_data_dir(tmp_path):
    env = {"MONITOR_WALLET": WALLET, "MONITOR_DAILY_LOSS_USDC": "100",
           "MONITOR_TIMEZONE": "Asia/Singapore", "MONITOR_MAX_POSITION_NOTIONAL_USDC": "500"}
    data_dir = tmp_path / "data"
    rw.configure_from_env(data_dir, env=env)
    written = json.loads((data_dir / "config.json").read_text())
    assert written["wallet"] == WALLET
    assert written["daily_loss_usdc"] == 100
    assert written["max_position_notional_usdc"] == 500


def test_configure_from_env_missing_wallet_exits_nonzero_with_a_clear_message():
    with pytest.raises(SystemExit) as excinfo:
        rw.configure_from_env("unused", env={"MONITOR_DAILY_LOSS_USDC": "100"})
    assert "MONITOR_WALLET" in str(excinfo.value)


def test_resolve_data_dir_defaults_to_account_monitor_default():
    import account_monitor as monitor
    assert rw.resolve_data_dir(env={}) == monitor.DEFAULT_DIR


def test_resolve_data_dir_honors_data_dir_env():
    from pathlib import Path
    assert rw.resolve_data_dir(env={"DATA_DIR": "/mnt/volume"}) == Path("/mnt/volume")


# ---------------------------------------------------------------------------
# /report — a fake connection, no real socket bind (this sandbox refuses one)
# ---------------------------------------------------------------------------

class FakeConnection:
    def __init__(self, request_bytes):
        self._rfile = io.BytesIO(request_bytes)
        self.sent = bytearray()

    def makefile(self, mode, bufsize=-1):
        if "r" in mode:
            return self._rfile
        raise AssertionError(f"unexpected makefile mode: {mode}")

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, *_args):
        pass

    def setsockopt(self, *_args):
        pass

    def fileno(self):
        return -1


def invoke(handler_cls, request_bytes):
    connection = FakeConnection(request_bytes)
    handler_cls(connection, ("127.0.0.1", 1), None)
    return bytes(connection.sent)


def status_line(response):
    return response.split(b"\r\n", 1)[0]


def test_report_endpoint_returns_503_when_token_unset():
    state = rw.ReportState()
    state.update(1, {"status": "CLEAR"})
    handler = rw.make_handler(state, None)
    response = invoke(handler, b"GET /report HTTP/1.1\r\nAuthorization: Bearer anything\r\n\r\n")
    assert b"503" in status_line(response)


def test_report_endpoint_returns_401_with_no_token_header():
    state = rw.ReportState()
    state.update(1, {"status": "CLEAR"})
    handler = rw.make_handler(state, "correct-token")
    response = invoke(handler, b"GET /report HTTP/1.1\r\n\r\n")
    assert b"401" in status_line(response)


def test_report_endpoint_returns_401_with_wrong_token():
    state = rw.ReportState()
    state.update(1, {"status": "CLEAR"})
    handler = rw.make_handler(state, "correct-token")
    response = invoke(handler, b"GET /report HTTP/1.1\r\nAuthorization: Bearer wrong-token\r\n\r\n")
    assert b"401" in status_line(response)


def test_report_endpoint_returns_the_latest_report_with_the_right_token():
    state = rw.ReportState()
    state.update(12345, {"status": "CLEAR", "entry_allowed": True})
    handler = rw.make_handler(state, "correct-token")
    response = invoke(handler, b"GET /report HTTP/1.1\r\nAuthorization: Bearer correct-token\r\n\r\n")
    assert b"200" in status_line(response)
    body = response.split(b"\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["produced_at_ms"] == 12345
    assert payload["report"]["status"] == "CLEAR"


def test_report_endpoint_503_when_no_report_produced_yet():
    state = rw.ReportState()
    handler = rw.make_handler(state, "correct-token")
    response = invoke(handler, b"GET /report HTTP/1.1\r\nAuthorization: Bearer correct-token\r\n\r\n")
    assert b"503" in status_line(response)


def test_health_endpoint_is_unauthenticated_and_serves_no_data():
    state = rw.ReportState()
    handler = rw.make_handler(state, None)
    response = invoke(handler, b"GET /health HTTP/1.1\r\n\r\n")
    assert b"200" in status_line(response)
    assert response.endswith(b"ok")


def test_unknown_path_is_404():
    state = rw.ReportState()
    handler = rw.make_handler(state, "token")
    response = invoke(handler, b"GET /orders HTTP/1.1\r\nAuthorization: Bearer token\r\n\r\n")
    assert b"404" in status_line(response)


# ---------------------------------------------------------------------------
# Background delivery: the outbox, the pinger, and the daily alive summary
# ---------------------------------------------------------------------------

def test_outbox_keeps_a_failed_message_and_retries_it_in_order():
    up = {"ok": False}
    delivered = []

    def deliver(message):
        if not up["ok"]:
            return False
        delivered.append(message)
        return True

    box = rw.Outbox(deliver)
    box.put("ALL CLEAR: HYPE long position stop coverage is unprotected (0/10)")
    box.put("PROBLEM: HYPE added to a losing long")
    assert box.drain_once() is False                 # Telegram down: nothing lost
    assert len(box.pending()) == 2
    up["ok"] = True
    assert box.drain_once() is True
    assert delivered == ["ALL CLEAR: HYPE long position stop coverage is unprotected (0/10)",
                         "PROBLEM: HYPE added to a losing long"]
    assert box.pending() == []


def test_outbox_treats_a_raising_delivery_as_a_failure():
    box = rw.Outbox(lambda message: 1 / 0)
    box.put("x")
    assert box.drain_once() is False
    assert box.pending() == ["x"]


def test_outbox_drops_the_oldest_message_when_full(caplog):
    box = rw.Outbox(lambda message: False, max_pending=2)
    for message in ("a", "b", "c"):
        box.put(message)
    assert box.pending() == ["b", "c"]
    assert "dropped the oldest" in caplog.text


def test_background_pinger_keeps_at_most_one_ping_in_flight():
    import threading
    release = threading.Event()
    started = []

    def slow_ping():
        started.append(1)
        release.wait(2)

    pinger = rw.BackgroundPinger(slow_ping)
    pinger()
    pinger()                                         # previous still running: skipped
    release.set()
    for _ in range(100):
        if pinger._busy.acquire(blocking=False):
            pinger._busy.release()
            break
        import time
        time.sleep(0.01)
    assert started == [1]


def test_loop_survives_a_report_that_cannot_be_published():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 2)
    clocks = iter([MIDNIGHT + i * 30_000 for i in range(10)])

    def broken_publish(now_ms, report):
        raise ValueError("not serializable")

    pings = []
    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=lambda message: None, ping=lambda: pings.append(1), on_report=broken_publish,
            clock=lambda: next(clocks), sleep=lambda s: None, iterations=2)
    assert len(pings) == 2


def test_alive_summary_fires_once_at_nine_local_and_not_on_deploy_day():
    nine_sgt = int(datetime.fromisoformat("2026-09-08T09:00:00+08:00").timestamp() * 1000)
    w = make_watcher()
    w.boot(nine_sgt - 86_400_000 + 3_600_000)         # deployed 10:00 the day before
    report = {"status": "CLEAR", "observation": {"equity_usdc": 5145.07}}
    assert w.alive_summary(report, nine_sgt - 86_400_000 + 7_200_000) == []   # deploy day
    assert w.alive_summary(report, nine_sgt - 60_000) == []                   # 08:59
    assert w.alive_summary(report, nine_sgt) == [
        "watcher alive — status CLEAR | equity 5,145.07 USDC"]
    assert w.alive_summary(report, nine_sgt + 30_000) == []                   # once a day


def test_alive_summary_counts_recorder_problems_apart_from_account_problems():
    nine_sgt = int(datetime.fromisoformat("2026-09-08T09:00:00+08:00").timestamp() * 1000)
    w = make_watcher()
    w.boot(nine_sgt - 86_400_000 + 3_600_000)
    w.active = {"RECORDER_SILENT": {}, "RECORDER_UPLOAD_FAILING": {}}
    report = {"status": "CLEAR", "observation": {"equity_usdc": 5145.07}}
    assert w.alive_summary(report, nine_sgt) == [
        "watcher alive — status CLEAR | equity 5,145.07 USDC | 2 recorder problem(s) active"]
    w.active["HALT"] = {}
    w.last_alive_date = None
    assert w.alive_summary(report, nine_sgt + 86_400_000) == [
        "watcher alive — status CLEAR | equity 5,145.07 USDC"
        " | 1 account problem(s) active | 2 recorder problem(s) active"]


# ---------------------------------------------------------------------------
# Cross-review round 1: no all-clear without evidence
# ---------------------------------------------------------------------------

def test_unknown_open_risk_does_not_clear_an_active_excess_risk_alert():
    w = make_watcher()
    _, state = start_day()
    report, _ = observe(snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10,
                                 orders=[stop(trigger=60)]), state)
    w.step(report, MIDNIGHT + 1000)
    assert "EXCESS_OPEN_RISK" in w.active
    # Another position loses its stop: the monitor reports risk as unknown.
    unknown = {**report, "open_trigger_distance_risk_usdc": None}
    messages = w.step(unknown, MIDNIGHT + 2000)
    assert not any(m.startswith("ALL CLEAR") and "trigger-distance" in m for m in messages)
    assert "EXCESS_OPEN_RISK" in w.active


def test_unsupported_read_does_not_announce_monitor_recovery():
    from account_risk import failure_result
    w = make_watcher()
    for minutes in range(6):
        w.step(failure_result("offline"), MIDNIGHT + minutes * 60_000)
    assert "MONITOR_FAILURE" in w.active
    unsupported, _ = observe(snapshot(now=MIDNIGHT + 6 * 60_000, mode="portfolioMargin"))
    messages = w.step(unsupported, MIDNIGHT + 6 * 60_000)
    assert not any(m.startswith("ALL CLEAR") for m in messages)
    assert "MONITOR_FAILURE" in w.active
    recovered, _ = start_day()
    messages = w.step(recovered, MIDNIGHT + 7 * 60_000)
    assert any(m.startswith("ALL CLEAR") and "not produced a reading" in m for m in messages)


# ---------------------------------------------------------------------------
# Recorder check (PR C): optional, driven by RECORDER_STATUS_URL — a
# separate problem group (RECORDER_*) that never touches account problems,
# /report or entry_allowed.
# ---------------------------------------------------------------------------

def _recorder_status(**overrides):
    base = {"connected": True, "seconds_since_last_message": 1.0, "uploads_enabled": True,
            "seconds_since_oldest_unconfirmed_day_ended": None, "seconds_upload_failing": None}
    base.update(overrides)
    return base


def test_recorder_step_healthy_status_raises_nothing():
    w = make_watcher()
    messages = w.recorder_step(_recorder_status(), MIDNIGHT)
    assert messages == []
    assert w.active == {}


def test_recorder_silent_appears_only_after_5_minutes_sustained_then_clears():
    w = make_watcher()
    unreachable = w.recorder_step(None, MIDNIGHT)
    assert unreachable == []  # first bad poll: not yet sustained 5 minutes
    assert "RECORDER_SILENT" not in w.active

    too_soon = w.recorder_step(None, MIDNIGHT + 4 * 60_000)
    assert too_soon == []
    assert "RECORDER_SILENT" not in w.active

    appear = w.recorder_step(None, MIDNIGHT + 5 * 60_000)
    assert any("PROBLEM:" in m and "recorder" in m for m in appear)
    assert "RECORDER_SILENT" in w.active

    cleared = w.recorder_step(_recorder_status(), MIDNIGHT + 6 * 60_000)
    assert any("ALL CLEAR" in m for m in cleared)
    assert "RECORDER_SILENT" not in w.active


def test_recorder_silent_repeats_every_30_minutes_while_it_lasts():
    w = make_watcher()
    for m in range(6):
        w.recorder_step(None, MIDNIGHT + m * 60_000)  # appears at +5 min
    assert "RECORDER_SILENT" in w.active

    quiet = w.recorder_step(None, MIDNIGHT + 34 * 60_000)  # 29 min after the appear alert
    assert quiet == []

    repeat = w.recorder_step(None, MIDNIGHT + 35 * 60_000)  # 30 min after the appear alert
    assert any(m.startswith("PROBLEM (ongoing)") and "recorder" in m for m in repeat)
    assert "RECORDER_SILENT" in w.active


def test_recorder_silent_from_not_connected_or_stale_message_not_just_unreachable():
    w = make_watcher()
    w.recorder_step(_recorder_status(connected=False), MIDNIGHT)
    w.recorder_step(_recorder_status(connected=False), MIDNIGHT + 5 * 60_000)
    assert "RECORDER_SILENT" in w.active  # reachable and "connected: false" still counts as silent

    w2 = make_watcher()
    w2.recorder_step(_recorder_status(seconds_since_last_message=301), MIDNIGHT)
    w2.recorder_step(_recorder_status(seconds_since_last_message=301), MIDNIGHT + 5 * 60_000)
    assert "RECORDER_SILENT" in w2.active  # connected, but no message in over 5 minutes


def test_recorder_silent_timer_resets_on_a_healthy_poll_in_between():
    w = make_watcher()
    w.recorder_step(None, MIDNIGHT)
    w.recorder_step(_recorder_status(), MIDNIGHT + 2 * 60_000)  # healthy poll resets the timer
    w.recorder_step(None, MIDNIGHT + 3 * 60_000)
    messages = w.recorder_step(None, MIDNIGHT + 7 * 60_000)  # only 4 min since the reset
    assert "RECORDER_SILENT" not in w.active
    assert not any("RECORDER" in m or "recorder" in m for m in messages)


def test_recorder_upload_failing_appears_past_24h_and_clears_when_caught_up():
    w = make_watcher()
    appear = w.recorder_step(_recorder_status(seconds_upload_failing=24 * 3600 + 1), MIDNIGHT)
    assert any("PROBLEM" in m and "upload" in m for m in appear)
    assert "RECORDER_UPLOAD_FAILING" in w.active

    cleared = w.recorder_step(_recorder_status(), MIDNIGHT + 2000)
    assert any("ALL CLEAR" in m for m in cleared)
    assert "RECORDER_UPLOAD_FAILING" not in w.active


def test_recorder_upload_failing_from_stale_unconfirmed_day_not_just_failing_since():
    w = make_watcher()
    status = _recorder_status(seconds_since_oldest_unconfirmed_day_ended=24 * 3600 + 50)
    messages = w.recorder_step(status, MIDNIGHT)
    assert any("PROBLEM" in m and "upload" in m for m in messages)
    assert "RECORDER_UPLOAD_FAILING" in w.active


def test_recorder_upload_failing_appears_immediately_when_uploads_disabled():
    # Missing S3 config is a definite, immediate condition — like
    # CONFIG_UNSUPPORTED for account problems — not something that needs a
    # 24h sustain window.
    w = make_watcher()
    messages = w.recorder_step(_recorder_status(uploads_enabled=False), MIDNIGHT)
    assert any("PROBLEM" in m and "disabled" in m for m in messages)
    assert "RECORDER_UPLOAD_FAILING" in w.active

    cleared = w.recorder_step(_recorder_status(uploads_enabled=True), MIDNIGHT + 1000)
    assert any("ALL CLEAR" in m for m in cleared)
    assert "RECORDER_UPLOAD_FAILING" not in w.active


def test_recorder_upload_failing_untouched_on_an_unreachable_poll():
    w = make_watcher()
    w.recorder_step(_recorder_status(seconds_upload_failing=24 * 3600 + 1), MIDNIGHT)
    assert "RECORDER_UPLOAD_FAILING" in w.active
    # An unreachable poll has no evidence either way — must not clear it.
    w.recorder_step(None, MIDNIGHT + 1000)
    assert "RECORDER_UPLOAD_FAILING" in w.active


def test_recorder_problems_never_touch_account_problems():
    w = make_watcher()
    _, state = observe(snapshot(now=MIDNIGHT - 20_000))
    unprotected, _ = observe(snapshot(now=MIDNIGHT, size=10), state)
    w.step(unprotected, MIDNIGHT)
    assert "STOP:HYPE" in w.active

    w.recorder_step(None, MIDNIGHT)
    w.recorder_step(None, MIDNIGHT + 5 * 60_000)
    assert "RECORDER_SILENT" in w.active
    assert "STOP:HYPE" in w.active  # untouched by the recorder check

    w.recorder_step(_recorder_status(), MIDNIGHT + 6 * 60_000)
    assert "RECORDER_SILENT" not in w.active
    assert "STOP:HYPE" in w.active  # still untouched


def test_fetch_recorder_status_returns_none_on_any_failure():
    def timeout_get(url, timeout=None):
        raise requests.exceptions.Timeout("slow")
    assert rw.fetch_recorder_status("http://x/status", get=timeout_get) is None

    def bad_status_get(url, timeout=None):
        class R:
            status_code = 500
            def raise_for_status(self):
                raise requests.exceptions.HTTPError("500")
        return R()
    assert rw.fetch_recorder_status("http://x/status", get=bad_status_get) is None

    def non_dict_get(url, timeout=None):
        class R:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return [1, 2, 3]
        return R()
    assert rw.fetch_recorder_status("http://x/status", get=non_dict_get) is None


def test_fetch_recorder_status_returns_the_parsed_body_on_success():
    def get(url, timeout=None):
        class R:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {"connected": True}
        return R()
    assert rw.fetch_recorder_status("http://x/status", get=get) == {"connected": True}


def test_loop_recorder_feature_off_when_url_unset_zero_behaviour_change():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 2)
    clocks = iter([MIDNIGHT + i * 30_000 for i in range(10)])
    fetch_calls = []

    def fetch(url):
        fetch_calls.append(url)
        return None

    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=lambda m: None, ping=lambda: None, clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=2, recorder_status_url=None,
            fetch_recorder_status=fetch, recorder_spawn=_run_now)
    assert fetch_calls == []


def _clock_sequence(top_values, calls_per_iter=4):
    """`loop()` calls `clock()` up to `calls_per_iter` times per iteration
    (once at the top, captured as `now_ms`, once more just before the
    recorder poll, then twice for the sleep calculation on every non-final
    pass). This repeats each wanted per-iteration value across all of that
    iteration's calls."""
    seq = []
    for v in top_values:
        seq.extend([v] * calls_per_iter)
    return iter(seq)


def _run_now(target):
    """Synchronous stand-in for the poller's background thread."""
    target()


def test_loop_is_not_stalled_by_a_hung_recorder_poll():
    # Codex r1: a trickling /status body defeats the per-read timeout. The
    # poll runs on its own thread, so the account loop keeps going.
    release = threading.Event()
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 3)
    clocks = _clock_sequence([MIDNIGHT + i * 60_000 for i in range(3)])
    pings = []

    def hanging_fetch(url):
        release.wait(5)
        return None

    try:
        rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
                send=lambda m: None, ping=lambda: pings.append(1), clock=lambda: next(clocks),
                sleep=lambda s: None, iterations=3, recorder_status_url="http://x/status",
                fetch_recorder_status=hanging_fetch)
        assert len(pings) == 3  # every account pass finished while the poll hung
    finally:
        release.set()


def test_a_stuck_recorder_poll_reads_as_unreachable_and_alerts():
    started = []
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 10)
    clocks = _clock_sequence([MIDNIGHT + i * 60_000 for i in range(10)])
    sent = []
    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=sent.append, ping=lambda: None, clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=10, recorder_status_url="http://x/status",
            fetch_recorder_status=lambda url: {"connected": True},
            recorder_spawn=started.append)  # the poll never finishes
    assert len(started) == 1  # never a second poll while one is in flight
    assert any("PROBLEM:" in m and "recorder" in m for m in sent)


def test_loop_ages_recorder_replies_against_the_clock_after_the_account_check():
    # Codex r3: now_ms is read before the account check, which can take tens
    # of seconds. A reply 130s old must still be read as unreachable.
    t = {"now": MIDNIGHT}
    threads = []
    seen = []

    class RecordingWatcher(type(make_watcher())):
        def recorder_step(self, status, now_ms):
            seen.append(status)
            return super().recorder_step(status, now_ms)

    watcher = make_watcher()
    watcher.__class__ = RecordingWatcher

    def slow_check(_dir):
        if threads:           # 2nd pass: the check is slow, and the reply lands meanwhile
            t["now"] += 130_000
            threads[0]()
        return {"status": "DATA_UNAVAILABLE", "reasons": ["x"]}

    def sleep(_s):
        t["now"] += 90_000

    rw.loop(watcher, data_dir="unused", check=slow_check, send=lambda m: None,
            ping=lambda: None, clock=lambda: t["now"], sleep=sleep, iterations=2,
            recorder_status_url="http://x/status",
            fetch_recorder_status=lambda url: {"connected": True, "seconds_since_last_message": 1.0},
            recorder_spawn=threads.append)
    assert seen == [None]  # the 220s-old healthy reply was never passed on as healthy


def test_poller_reads_a_stuck_poll_as_unreachable_once_per_interval():
    threads = []
    poller = rw.RecorderPoller("http://x/status", lambda url: {"connected": True},
                               interval_s=60, stuck_after_s=120, spawn=threads.append)
    assert poller.tick(0) == []
    assert poller.tick(119_000) == []
    assert poller.tick(120_000) == [None]      # stuck -> unreachable
    assert poller.tick(150_000) == []          # at most once per interval
    assert poller.tick(180_000) == [None]


def test_a_late_healthy_reply_never_clears_a_recorder_alert():
    # Codex r2: a healthy reply that trickled in after the poll was already
    # read as stuck described a moment long gone — it must not clear
    # RECORDER_SILENT; only a fresh poll may.
    threads = []
    watcher = make_watcher()
    poller = rw.RecorderPoller("http://x/status", lambda url: {"connected": True,
                                                               "seconds_since_last_message": 1.0},
                               interval_s=60, stuck_after_s=120, spawn=threads.append)
    sent = []
    t = MIDNIGHT
    for now in range(t, t + 481_000, 60_000):
        for status in poller.tick(now):
            sent += watcher.recorder_step(status, now)
    assert any("PROBLEM:" in m for m in sent)
    sent.clear()
    threads[0]()                                # the stale healthy reply lands
    assert poller.tick(t + 500_000) == [None]   # read as unreachable, not healthy
    assert len(threads) == 2                    # a fresh poll started on that same tick...
    threads[1]()
    status = poller.tick(t + 501_000)           # ...and its prompt reply counts
    assert status == [{"connected": True, "seconds_since_last_message": 1.0}]


def test_loop_polls_recorder_at_most_once_a_minute():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 4)
    # Four iterations 15s apart (60s total) — the account loop's own cadence
    # is faster than the recorder poll interval, so only the first pass polls.
    clocks = _clock_sequence([MIDNIGHT + i * 15_000 for i in range(4)])
    fetch_calls = []

    def fetch(url):
        fetch_calls.append(url)
        return {"connected": True, "seconds_since_last_message": 1.0}

    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=lambda m: None, ping=lambda: None, clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=4, recorder_status_url="http://x/status",
            fetch_recorder_status=fetch, recorder_spawn=_run_now)
    assert fetch_calls == ["http://x/status"]  # only the first of the four passes polled


def test_loop_polls_recorder_again_once_the_interval_elapses():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 5)
    # Five iterations 20s apart: the 4th pass (60s after the 1st) polls again.
    clocks = _clock_sequence([MIDNIGHT + i * 20_000 for i in range(5)])
    fetch_calls = []

    def fetch(url):
        fetch_calls.append(url)
        return {"connected": True, "seconds_since_last_message": 1.0}

    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=lambda m: None, ping=lambda: None, clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=5, recorder_status_url="http://x/status",
            fetch_recorder_status=fetch, recorder_spawn=_run_now)
    assert len(fetch_calls) == 2


def test_loop_recorder_check_exception_does_not_affect_account_checks():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 2)
    clocks = iter([MIDNIGHT + i * 30_000 for i in range(10)])
    account_pings = []

    def raising_fetch(url):
        raise RuntimeError("boom")

    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=lambda m: None, ping=lambda: account_pings.append(1),
            clock=lambda: next(clocks), sleep=lambda s: None, iterations=2,
            recorder_status_url="http://x/status", fetch_recorder_status=raising_fetch, recorder_spawn=_run_now)
    assert len(account_pings) == 2  # both passes completed despite the recorder check raising


def test_loop_recorder_alerts_flow_through_send():
    reports = iter([{"status": "DATA_UNAVAILABLE", "reasons": ["x"]}] * 6)
    clocks = _clock_sequence([MIDNIGHT + i * 60_000 for i in range(6)])
    sent = []

    def fetch(url):
        return None  # unreachable every poll

    rw.loop(make_watcher(), data_dir="unused", check=lambda _dir: next(reports),
            send=sent.append, ping=lambda: None, clock=lambda: next(clocks),
            sleep=lambda s: None, iterations=6, recorder_status_url="http://x/status",
            fetch_recorder_status=fetch, recorder_spawn=_run_now)
    assert any("PROBLEM:" in m and "recorder" in m for m in sent)
