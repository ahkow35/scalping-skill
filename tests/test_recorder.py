import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import recorder as rec


MIDNIGHT = (1779100800000 // 86_400_000) * 86_400_000  # a UTC midnight, ms
DAY_MS = 86_400_000


# ── UTC stamping (regression: this repo has a timezone-bug history) ────────

def test_iso_utc_ms_is_utc_not_local(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    # 2026-05-18 11:12:00.123 UTC
    ms = 1779102720123
    assert rec.iso_utc_ms(ms) == "2026-05-18T11:12:00.123Z"


def test_iso_utc_ms_ignores_machine_timezone_variants(monkeypatch):
    ms = 1779102720000
    out_utc = rec.iso_utc_ms(ms)
    for tz in ("UTC", "America/New_York", "Asia/Singapore", "Pacific/Kiritimati"):
        monkeypatch.setenv("TZ", tz)
        assert rec.iso_utc_ms(ms) == out_utc


def test_day_str_utc_matches_utc_calendar_day():
    # one ms before UTC midnight is the prior day; exactly midnight is the new day
    assert rec.day_str_utc(MIDNIGHT - 1) != rec.day_str_utc(MIDNIGHT)
    assert rec.day_str_utc(MIDNIGHT) == rec.day_str_utc(MIDNIGHT + 1000)


def test_data_record_carries_both_timestamps_explicitly_utc():
    r = rec.make_data_record("HYPE", "trades", {"tid": 1}, exchange_ts_ms=MIDNIGHT,
                              recv_ms=MIDNIGHT + 50)
    assert r["exchange_ts_utc"] == rec.iso_utc_ms(MIDNIGHT)
    assert r["recv_ts_utc"] == rec.iso_utc_ms(MIDNIGHT + 50)
    assert r["exchange_ts_ms"] == MIDNIGHT
    assert r["recv_ts_ms"] == MIDNIGHT + 50
    assert r["data"] == {"tid": 1}  # verbatim — no field dropped


# ── dedupe ───────────────────────────────────────────────────────────────

def test_dedupe_trade_true_once_then_false():
    seen = set()
    assert rec.dedupe_trade(seen, "tid-1") is True
    assert rec.dedupe_trade(seen, "tid-1") is False
    assert rec.dedupe_trade(seen, "tid-2") is True


def test_recorder_dedupes_trades_by_tid_writes_only_once(tmp_path):
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    trade = {"coin": "HYPE", "tid": "abc", "time": MIDNIGHT, "side": "B",
             "px": "10.0", "sz": "1.0", "users": ["0xbuyer", "0xseller"]}
    fr.handle_trade("HYPE", trade)
    clock["t"] += 1000
    fr.handle_trade("HYPE", dict(trade))  # same tid, "fresh" message (e.g. resend)
    clock["t"] += 1000
    trade2 = dict(trade, tid="xyz")
    fr.handle_trade("HYPE", trade2)
    fr.files.close_all()

    path = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(MIDNIGHT))
    with open(path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    tids = [line["data"]["tid"] for line in lines]
    assert tids == ["abc", "xyz"]  # the duplicate "abc" resend was dropped


def test_recorder_preserves_users_buyer_seller_field(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    trade = {"coin": "HYPE", "tid": "abc", "time": MIDNIGHT, "side": "B",
             "px": "10.0", "sz": "1.0", "users": ["0xbuyer", "0xseller"]}
    fr.handle_trade("HYPE", trade)
    fr.files.close_all()
    path = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(MIDNIGHT))
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert rows[0]["data"]["users"] == ["0xbuyer", "0xseller"]


# ── GAP records ──────────────────────────────────────────────────────────

def test_emit_gaps_writes_one_record_per_coin_channel(tmp_path):
    fr = rec.FlowRecorder(["HYPE", "BTC"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.emit_gaps(reconnect_ms=MIDNIGHT + 5000)
    fr.files.close_all()
    for coin in ("HYPE", "BTC"):
        for channel in rec.CHANNELS:
            path = rec.file_path(str(tmp_path), coin, channel, rec.day_str_utc(MIDNIGHT))
            with open(path) as f:
                rows = [json.loads(line) for line in f if line.strip()]
            assert len(rows) == 1
            assert rows[0]["record_type"] == "gap"
            assert rows[0]["coin"] == coin
            assert rows[0]["channel"] == channel
            assert rows[0]["reconnect_ts_ms"] == MIDNIGHT + 5000
            assert rows[0]["last_seen_exchange_ts_ms"] is None  # cold start


def test_emit_gaps_carries_last_seen_exchange_ts_after_data(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.handle_snapshot("l2Book", "HYPE", {"coin": "HYPE", "time": MIDNIGHT + 100, "levels": [[], []]})
    fr.emit_gaps(reconnect_ms=MIDNIGHT + 9000)
    fr.files.close_all()
    path = rec.file_path(str(tmp_path), "HYPE", "l2Book", rec.day_str_utc(MIDNIGHT))
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    data_rows = [r for r in rows if r["record_type"] == "l2Book"]
    gap_rows = [r for r in rows if r["record_type"] == "gap"]
    assert len(data_rows) == 1 and len(gap_rows) == 1
    assert gap_rows[0]["last_seen_exchange_ts_ms"] == MIDNIGHT + 100
    assert gap_rows[0]["last_seen_exchange_ts_utc"] == rec.iso_utc_ms(MIDNIGHT + 100)


def test_run_cold_start_emits_gap_marking_recording_started(tmp_path):
    # emit_gaps() is also called once before the connect loop in run() — a
    # fresh session with no prior data should still leave a marker, not
    # silence, in each file.
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.emit_gaps()
    fr.files.close_all()
    path = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(MIDNIGHT))
    assert os.path.exists(path)


# ── file rotation at UTC day boundary ───────────────────────────────────

def test_file_rotation_at_utc_day_boundary(tmp_path):
    clock = {"t": MIDNIGHT - 2000}  # just before midnight
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_trade("HYPE", {"coin": "HYPE", "tid": "before", "time": clock["t"],
                              "side": "B", "px": "10", "sz": "1", "users": ["a", "b"]})
    clock["t"] = MIDNIGHT + 2000  # just after midnight — new UTC day
    fr.handle_trade("HYPE", {"coin": "HYPE", "tid": "after", "time": clock["t"],
                              "side": "A", "px": "10", "sz": "1", "users": ["a", "b"]})
    fr.files.close_all()

    day_before = rec.day_str_utc(MIDNIGHT - 2000)
    day_after = rec.day_str_utc(MIDNIGHT + 2000)
    assert day_before != day_after

    p_before = rec.file_path(str(tmp_path), "HYPE", "trades", day_before)
    p_after = rec.file_path(str(tmp_path), "HYPE", "trades", day_after)
    # The finished (before-midnight) file is gzipped on rotation; today's
    # (after-midnight) file stays plain.
    assert not os.path.exists(p_before)
    assert os.path.exists(p_before + ".gz")
    assert os.path.exists(p_after)

    rows_before = list(rec.iter_jsonl_records(p_before))
    rows_after = list(rec.iter_jsonl_records(p_after))
    assert [r["data"]["tid"] for r in rows_before] == ["before"]
    assert [r["data"]["tid"] for r in rows_after] == ["after"]


def test_flow_file_set_rotation_reopens_same_day_without_truncating(tmp_path):
    files = rec.FlowFileSet(str(tmp_path))
    day = rec.day_str_utc(MIDNIGHT)
    files.write("HYPE", "trades", {"n": 1}, day)
    files.write("HYPE", "trades", {"n": 2}, day)
    files.close_all()
    path = rec.file_path(str(tmp_path), "HYPE", "trades", day)
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert [r["n"] for r in rows] == [1, 2]


def test_startup_compresses_stale_plain_files_left_by_a_crash(tmp_path):
    # A plain file from a UTC day before "today" (as the wall clock, not the
    # test's fake `clock`, sees it) simulates a crash that never reached its
    # own rotation.
    stale_day = rec.day_str_utc(rec.now_ms() - 3 * DAY_MS)
    stale_path = rec.file_path(str(tmp_path), "HYPE", "trades", stale_day)
    os.makedirs(tmp_path, exist_ok=True)
    with open(stale_path, "w") as f:
        f.write(json.dumps({"n": 1}) + "\n")

    rec.FlowFileSet(str(tmp_path))  # __init__ should compress leftovers

    assert not os.path.exists(stale_path)
    assert os.path.exists(stale_path + ".gz")
    assert list(rec.iter_jsonl_records(stale_path)) == [{"n": 1}]


def test_startup_leaves_todays_plain_file_uncompressed(tmp_path):
    today = rec.day_str_utc(rec.now_ms())
    today_path = rec.file_path(str(tmp_path), "HYPE", "trades", today)
    os.makedirs(tmp_path, exist_ok=True)
    with open(today_path, "w") as f:
        f.write(json.dumps({"n": 1}) + "\n")

    rec.FlowFileSet(str(tmp_path))

    assert os.path.exists(today_path)
    assert not os.path.exists(today_path + ".gz")


def test_iter_jsonl_records_reads_gz_when_plain_missing(tmp_path):
    path = os.path.join(str(tmp_path), "f.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"a": 1}) + "\n")
        f.write(json.dumps({"a": 2}) + "\n")
    rec._gzip_file_atomic(path)
    assert not os.path.exists(path)
    assert list(rec.iter_jsonl_records(path)) == [{"a": 1}, {"a": 2}]


def test_iter_jsonl_records_empty_when_neither_form_exists(tmp_path):
    path = os.path.join(str(tmp_path), "missing.jsonl")
    assert list(rec.iter_jsonl_records(path)) == []


# ── watchdog: silent stream forces a reconnect ──────────────────────────

def test_recv_loop_raises_watchdog_timeout_when_stream_goes_silent(tmp_path):
    import asyncio

    class _SilentWS:
        """Mocks a half-open socket the way a real one behaves after Mac
        sleep: recv() never raises, never returns — it just never
        completes."""
        async def recv(self):
            await asyncio.Event().wait()

    async def _run():
        fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT,
                              watchdog_s=0.05)
        fr._stop = asyncio.Event()
        with pytest.raises(rec._WatchdogTimeout):
            await fr._recv_loop(_SilentWS())

    asyncio.run(_run())


def test_recv_loop_delivers_messages_within_watchdog_window(tmp_path):
    import asyncio

    class _OneMessageWS:
        def __init__(self):
            self._sent = False

        async def recv(self):
            if not self._sent:
                self._sent = True
                return json.dumps({"channel": "bbo", "data": {"coin": "HYPE", "time": MIDNIGHT}})
            await asyncio.Event().wait()  # silent after the one message

    async def _run():
        fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT,
                              watchdog_s=0.2)
        fr._stop = asyncio.Event()
        with pytest.raises(rec._WatchdogTimeout):
            await fr._recv_loop(_OneMessageWS())
        fr.files.close_all()

    asyncio.run(_run())
    path = rec.file_path(str(tmp_path), "HYPE", "bbo", rec.day_str_utc(MIDNIGHT))
    assert len(list(rec.iter_jsonl_records(path))) == 1


# ── disk floor: alert only, never delete ────────────────────────────────

def test_status_reports_disk_low_below_floor(tmp_path, monkeypatch):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT,
                          disk_floor_gb=999_999)  # unreasonably high floor -> always "low"
    st = fr.status()
    assert st["disk_low"] is True
    assert st["disk_free_gb"] is not None


def test_status_disk_not_low_when_floor_is_zero(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT,
                          disk_floor_gb=0)
    st = fr.status()
    assert st["disk_low"] is False


# ── status.json: reconnects/gap totals, per-stream lag ──────────────────

def test_status_tracks_reconnects_and_gap_seconds_in_24h(tmp_path):
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_snapshot("bbo", "HYPE", {"coin": "HYPE", "time": MIDNIGHT})
    fr.mark_disconnected()
    clock["t"] += 5000
    fr.end_outage(clock["t"])  # last recv was MIDNIGHT -> 5s gap
    st = fr.status()
    assert st["reconnects_24h"] == 1
    assert st["gap_seconds_24h"] == 5.0


def test_one_outage_counts_once_however_many_retries(tmp_path):
    # Regression: every failed retry used to count a reconnect and re-add the
    # gap from the same last-seen point (1+2+4+... seconds), and gap time was
    # summed per stream, so one outage was inflated many times over.
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_snapshot("bbo", "HYPE", {"coin": "HYPE", "time": MIDNIGHT})
    fr.mark_disconnected()
    for _ in range(5):  # failed retries inside the same outage
        clock["t"] += 10_000
        fr.mark_disconnected()
    clock["t"] += 10_000
    fr.end_outage(clock["t"])
    st = fr.status()
    assert st["reconnects_24h"] == 1
    assert st["gap_seconds_24h"] == 60.0


def test_end_outage_without_open_outage_is_a_noop(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.end_outage(MIDNIGHT)
    fr.files.close_all()
    st = fr.status()
    assert st["reconnects_24h"] == 0
    assert st["gap_seconds_24h"] == 0.0
    assert not os.listdir(tmp_path) or os.listdir(tmp_path) == ["status.json"]


def test_status_ignores_reconnects_older_than_24h(tmp_path):
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.mark_disconnected()
    fr.end_outage(clock["t"])
    clock["t"] += 25 * 3600 * 1000  # 25h later
    st = fr.status()
    assert st["reconnects_24h"] == 0


# ── run(): end to end against a fake websocket ─────────────────────────────

class _FakeWS:
    """Delivers `messages`, then either drops (raise) or asks the recorder to
    stop, mimicking a SIGTERM arriving mid-stream."""
    def __init__(self, fr, messages, then):
        self._fr, self._msgs, self._then = fr, list(messages), then

    async def send(self, _):
        pass

    async def recv(self):
        if self._msgs:
            return json.dumps(self._msgs.pop(0))
        if self._then == "drop":
            raise ConnectionError("socket dropped")
        self._fr._stop.set()
        import asyncio
        await asyncio.Event().wait()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_with_script(tmp_path, monkeypatch, clock, script):
    """script: list of ("fail" | "fail_and_stop", t_ms) or
    ("ws", t_ms, messages, then), consumed one per connect attempt; the clock
    jumps to t_ms at each attempt."""
    import asyncio
    import types
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    steps = list(script)

    def connect(*_a, **_k):
        step = steps.pop(0)
        clock["t"] = step[1]
        if step[0] == "fail_and_stop":
            fr._stop.set()
        if step[0] in ("fail", "fail_and_stop"):
            raise OSError("network unreachable")
        return _FakeWS(fr, step[2], step[3])

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(rec, "RECONNECT_BASE_S", 0.001)
    monkeypatch.setattr(rec, "RECONNECT_MAX_S", 0.001)
    asyncio.run(fr.run())
    with open(rec.status_path_for(str(tmp_path))) as f:
        return fr, json.load(f)


def _bbo(t):
    return {"channel": "bbo", "data": {"coin": "HYPE", "time": t}}


def test_run_long_outage_counts_one_reconnect_and_real_gap(tmp_path, monkeypatch):
    clock = {"t": MIDNIGHT}
    fr, st = _run_with_script(tmp_path, monkeypatch, clock, [
        ("ws", MIDNIGHT, [_bbo(MIDNIGHT)], "drop"),
        ("fail", MIDNIGHT + 10_000),
        ("fail", MIDNIGHT + 20_000),
        ("fail", MIDNIGHT + 30_000),
        ("fail", MIDNIGHT + 40_000),
        ("ws", MIDNIGHT + 90_000, [_bbo(MIDNIGHT + 90_000)], "stop"),
    ])
    assert st["reconnects_24h"] == 1
    assert st["gap_seconds_24h"] == 90.0
    # GAP records: the cold-start marker plus one for the single outage.
    path = rec.file_path(str(tmp_path), "HYPE", "bbo", rec.day_str_utc(MIDNIGHT))
    gaps = [r for r in rec.iter_jsonl_records(path) if r["record_type"] == "gap"]
    assert len(gaps) == 2


def test_run_clean_stop_writes_connected_false(tmp_path, monkeypatch):
    clock = {"t": MIDNIGHT}
    fr, st = _run_with_script(tmp_path, monkeypatch, clock, [
        ("ws", MIDNIGHT, [_bbo(MIDNIGHT)], "stop"),
    ])
    assert st["connected"] is False
    assert st["reconnects_24h"] == 0
    assert st["gap_seconds_24h"] == 0.0


def test_run_stop_mid_outage_records_gap_but_no_reconnect(tmp_path, monkeypatch):
    clock = {"t": MIDNIGHT}
    fr, st = _run_with_script(tmp_path, monkeypatch, clock, [
        ("ws", MIDNIGHT, [_bbo(MIDNIGHT)], "drop"),
        ("fail", MIDNIGHT + 10_000),
        ("fail_and_stop", MIDNIGHT + 45_000),  # SIGTERM while still down
    ])
    assert st["connected"] is False
    assert st["reconnects_24h"] == 0
    assert st["gap_seconds_24h"] == 45.0
    path = rec.file_path(str(tmp_path), "HYPE", "bbo", rec.day_str_utc(MIDNIGHT))
    gaps = [r for r in rec.iter_jsonl_records(path) if r["record_type"] == "gap"]
    assert len(gaps) == 2  # cold start + the outage closed at shutdown


def test_status_lag_s_per_stream(tmp_path):
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_snapshot("bbo", "HYPE", {"coin": "HYPE", "time": MIDNIGHT})
    clock["t"] += 3000
    st = fr.status()
    assert st["lag_s_per_stream"]["HYPE:bbo"] == 3.0


# ── UTC-day-roll tid dedupe clear ────────────────────────────────────────

def test_dedupe_set_clears_at_utc_day_roll_and_allows_same_tid_again(tmp_path):
    clock = {"t": MIDNIGHT - 1000}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    trade = {"coin": "HYPE", "tid": "dup", "time": clock["t"], "side": "B",
              "px": "1", "sz": "1", "users": ["a", "b"]}
    fr.handle_trade("HYPE", dict(trade))
    clock["t"] = MIDNIGHT + 1000  # new UTC day
    # HL replays trades on subscribe/reconnect; the same tid can legitimately
    # resurface right at a day boundary — the fresh day's dedupe set must not
    # still be carrying yesterday's tids and silently drop it.
    fr.handle_trade("HYPE", dict(trade, time=clock["t"]))
    fr.files.close_all()

    day_before = rec.day_str_utc(MIDNIGHT - 1000)
    day_after = rec.day_str_utc(MIDNIGHT + 1000)
    rows_before = list(rec.iter_jsonl_records(
        rec.file_path(str(tmp_path), "HYPE", "trades", day_before)))
    rows_after = list(rec.iter_jsonl_records(
        rec.file_path(str(tmp_path), "HYPE", "trades", day_after)))
    assert len(rows_before) == 1
    assert len(rows_after) == 1


# ── dispatch_message routing ────────────────────────────────────────────

def test_dispatch_message_routes_trades_l2book_bbo(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.dispatch_message({"channel": "trades", "data": [
        {"coin": "HYPE", "tid": "t1", "time": MIDNIGHT, "side": "B",
         "px": "1", "sz": "1", "users": ["a", "b"]}]})
    fr.dispatch_message({"channel": "l2Book", "data": {"coin": "HYPE", "time": MIDNIGHT}})
    fr.dispatch_message({"channel": "bbo", "data": {"coin": "HYPE", "time": MIDNIGHT}})
    fr.dispatch_message({"channel": "subscriptionResponse", "data": {}})  # ignored
    fr.dispatch_message({"channel": "pong"})  # ignored, no crash
    fr.files.close_all()
    for channel in rec.CHANNELS:
        path = rec.file_path(str(tmp_path), "HYPE", channel, rec.day_str_utc(MIDNIGHT))
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        assert len(rows) == 1


def test_dispatch_message_ignores_untracked_coin(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.dispatch_message({"channel": "trades", "data": [
        {"coin": "BTC", "tid": "t1", "time": MIDNIGHT, "side": "B",
         "px": "1", "sz": "1", "users": ["a", "b"]}]})
    fr.files.close_all()
    assert fr._counts == {}


# ── status ───────────────────────────────────────────────────────────────

def test_build_status_lag_and_counts():
    st = rec.build_status({"HYPE:trades": 3}, last_recv_ms=MIDNIGHT,
                          connected=True, now=MIDNIGHT + 2500)
    assert st["connected"] is True
    assert st["lag_s"] == 2.5
    assert st["counts"] == {"HYPE:trades": 3}
    json.dumps(st)  # must be JSON-serializable — this is what write_status() dumps


def test_build_status_no_data_yet_has_none_lag():
    st = rec.build_status({}, last_recv_ms=None, connected=False, now=MIDNIGHT)
    assert st["lag_s"] is None
    assert st["connected"] is False


# ── --verify: recorded sample_pct vs candle n ───────────────────────────

def test_verify_sample_pct_computes_against_candle_n(tmp_path, monkeypatch):
    import fetch_market as fm

    win_start = MIDNIGHT
    now = MIDNIGHT + 5 * 60_000
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: win_start)
    # one trade per minute; the last one sits in a partial edge minute and
    # must be excluded from the comparison (whole-minute spans only)
    for i in range(5):
        fr.handle_trade("HYPE", {"coin": "HYPE", "tid": f"t{i}", "time": win_start + i * 60_000,
                                  "side": "B", "px": "1", "sz": "1", "users": ["a", "b"]})
    fr.files.close_all()

    monkeypatch.setattr(fm, "fetch_candles", lambda coin, iv, s, e: [
        {"t": fm.iso_utc(win_start + i * 60_000), "t_ms": win_start + i * 60_000,
         "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "n": 100} for i in range(5)])

    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=5, now=now)
    assert result["compared_span_min"] == 4.0  # trailing partial minute trimmed
    assert result["recorded_trades"] == 4      # trade in the trimmed minute excluded
    assert result["candle_n_sum"] == 400       # candle in the trimmed minute excluded
    assert result["sample_pct"] == 1.0  # 4/400


def test_verify_sample_pct_none_when_no_candle_data(tmp_path, monkeypatch):
    import fetch_market as fm
    monkeypatch.setattr(fm, "fetch_candles", lambda coin, iv, s, e: [])
    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=5, now=MIDNIGHT)
    assert result["sample_pct"] is None
    assert result["recorded_trades"] == 0


def test_verify_sample_pct_against_real_fetch_candles_shape(tmp_path, monkeypatch):
    """Regression: verify_sample_pct calls fetch_market.fetch_candles (not a
    stub of it) and reads c['n'] from what that function actually returns.
    Mocks only the HTTP layer (_post_json) with HL's raw candleSnapshot
    field names (t/T/o/h/l/c/v/n), so a future field drop in fetch_candles'
    own dict-building code — the bug this test guards against, where main
    once omitted 'n' and the self-check silently read 0/None — is caught
    here instead of hidden by a fake candle dict that always includes it."""
    import fetch_market as fm

    win_start = MIDNIGHT
    now = MIDNIGHT + 5 * 60_000
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: win_start)
    for i in range(5):
        fr.handle_trade("HYPE", {"coin": "HYPE", "tid": f"t{i}", "time": win_start + i * 60_000,
                                  "side": "B", "px": "1", "sz": "1", "users": ["a", "b"]})
    fr.files.close_all()

    # Raw HL candleSnapshot rows, as the exchange actually sends them —
    # string OHLCV, int t/T/n — routed through the real fetch_candles().
    raw_rows = [{"t": win_start + i * 60_000, "T": win_start + (i + 1) * 60_000 - 1,
                 "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "n": 100}
                for i in range(5)]
    monkeypatch.setattr(fm, "_post_json", lambda url, payload, source: raw_rows)

    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=5, now=now)
    assert result["compared_span_min"] == 4.0
    assert result["recorded_trades"] == 4
    assert result["candle_n_sum"] == 400  # only recoverable if fetch_candles kept 'n'
    assert result["sample_pct"] == 1.0


# ── cross-review round 1 (Codex) regressions ────────────────────────────────

def _trade(tid, t):
    return {"coin": "HYPE", "tid": tid, "time": t, "side": "B", "px": "1", "sz": "1",
            "users": ["a", "b"]}


def test_clock_stepping_back_into_archived_day_never_overwrites_it(tmp_path):
    clock = {"t": MIDNIGHT - 2000}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_trade("HYPE", _trade("orig", clock["t"]))
    clock["t"] = MIDNIGHT + 1000          # roll: day D archived to .gz
    fr.handle_trade("HYPE", _trade("next1", clock["t"]))
    clock["t"] = MIDNIGHT - 1000          # clock steps back into day D
    fr.handle_trade("HYPE", _trade("late", clock["t"]))
    clock["t"] = MIDNIGHT + 3000          # roll again: D re-archived
    fr.handle_trade("HYPE", _trade("next2", clock["t"]))
    fr.files.close_all()
    day_d = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(MIDNIGHT - 1))
    assert not os.path.exists(day_d)
    assert [r["data"]["tid"] for r in rec.iter_jsonl_records(day_d)] == ["orig", "late"]


def test_restart_after_partial_line_keeps_the_gap_record(tmp_path):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT)
    fr.handle_snapshot("bbo", "HYPE", {"coin": "HYPE", "time": MIDNIGHT})
    fr.files.close_all()
    path = rec.file_path(str(tmp_path), "HYPE", "bbo", rec.day_str_utc(MIDNIGHT))
    with open(path, "a") as f:
        f.write('{"record_type": "bbo", "coin": "HY')   # crash mid-write
    fr2 = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: MIDNIGHT + 5000)
    fr2.emit_gaps()                                     # restart marker
    fr2.files.close_all()
    types = [r["record_type"] for r in rec.iter_jsonl_records(path)]
    assert types == ["bbo", "gap"]


def _five_trades(tmp_path, start):
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: start)
    for i in range(5):
        fr.handle_trade("HYPE", _trade(f"t{i}", start + i * 60_000))
    fr.files.close_all()


def test_verify_reports_unknown_when_a_candle_count_is_missing(tmp_path, monkeypatch):
    import fetch_market as fm
    _five_trades(tmp_path, MIDNIGHT)
    monkeypatch.setattr(fm, "fetch_candles", lambda coin, iv, s, e: [
        {"t_ms": MIDNIGHT + i * 60_000, "n": (None if i == 1 else 1)} for i in range(5)])
    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=5, now=MIDNIGHT + 5 * 60_000)
    assert result["sample_pct"] is None
    assert "1 of 4 minutes" in result["note"]


def test_verify_reports_unknown_when_a_candle_is_absent(tmp_path, monkeypatch):
    import fetch_market as fm
    _five_trades(tmp_path, MIDNIGHT)
    monkeypatch.setattr(fm, "fetch_candles", lambda coin, iv, s, e: [
        {"t_ms": MIDNIGHT + i * 60_000, "n": 1} for i in (0, 2, 3)])
    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=5, now=MIDNIGHT + 5 * 60_000)
    assert result["sample_pct"] is None


def test_verify_reads_every_day_of_a_multi_day_window(tmp_path, monkeypatch):
    import fetch_market as fm
    start = MIDNIGHT + DAY_MS - 60_000          # 1 min before the middle day
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: start)
    for i in range(4):                          # trades land on the middle day
        fr.handle_trade("HYPE", _trade(f"m{i}", MIDNIGHT + DAY_MS + i * 60_000))
    fr.files.close_all()
    # Records are filed by recv day; put them on the middle day explicitly.
    mid = rec.day_str_utc(MIDNIGHT + DAY_MS)
    src = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(start))
    rows = list(rec.iter_jsonl_records(src))
    for p in (src, src + ".gz"):
        if os.path.exists(p):
            os.remove(p)
    with open(rec.file_path(str(tmp_path), "HYPE", "trades", mid), "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    monkeypatch.setattr(fm, "fetch_candles", lambda coin, iv, s, e: [
        {"t_ms": t, "n": 1} for t in range(s, e, 60_000)])
    now = MIDNIGHT + 2 * DAY_MS + 60_000        # window spans days 0, 1, 2
    result = rec.verify_sample_pct(str(tmp_path), "HYPE", window_min=48 * 60, now=now)
    assert result["recorded_trades"] == 3       # whole minutes between first and last
    assert result["sample_pct"] == 100.0


def test_gap_seconds_clipped_to_window_and_includes_current_outage(tmp_path):
    clock = {"t": MIDNIGHT}
    fr = rec.FlowRecorder(["HYPE"], out_dir=str(tmp_path), clock=lambda: clock["t"])
    fr.handle_snapshot("bbo", "HYPE", {"coin": "HYPE", "time": MIDNIGHT})
    fr.mark_disconnected()
    clock["t"] += 3600_000                      # ongoing 1h outage
    assert fr.status()["gap_seconds_24h"] == 3600.0
    clock["t"] = MIDNIGHT + 25 * 3600_000       # 25h outage ends
    fr.end_outage(clock["t"])
    assert fr.status()["gap_seconds_24h"] == 24 * 3600.0
