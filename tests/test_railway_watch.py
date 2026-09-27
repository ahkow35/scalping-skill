import io
import json

import pytest

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


def test_classify_config_required_as_config():
    import account_monitor as monitor
    report = monitor.check("does-not-exist", fetch=lambda *a: pytest.fail("must not fetch"))
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
