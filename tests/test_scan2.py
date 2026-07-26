import json
import time

import audit_log
import card2
import scan2


def _snap(mark, oi, funding=1.25e-5):
    lvl = lambda p: {"t": 0, "o": str(p), "h": str(p * 1.001),
                     "l": str(p * 0.999), "c": str(p), "v": "5", "n": 50}
    candles = [lvl(mark * (1 + 0.002 * ((i % 5) - 2))) for i in range(40)]
    return {"ctx": {"mark": mark, "oi_usdc": oi, "funding": funding},
            "candles": {"15m": candles, "1h": candles},
            "btc_candles": {"1h": candles},
            "btc_ctx": {"mark": 65000.0, "funding": 1.25e-5},
            "taker_delta": {"15m": {"delta_usdc": -500.0,
                                    "coverage_pct": 12.0}},
            "session": {"utc": "x", "sgt": "y"}}


def test_run_scan_computes_window_deltas_and_writes_audit(tmp_path,
                                                          monkeypatch):
    calls = []

    def fake_assemble(coin, deep=False, now_ms=None):
        # first pass per coin: OI 100M; second pass: 101M, price +0.3%
        n = sum(1 for c in calls if c == coin)
        calls.append(coin)
        return _snap(58.0 * (1.003 if n else 1.0), 100e6 * (1.01 if n else 1.0))

    logged = []
    monkeypatch.setattr(scan2.audit_log, "write_audit_entry",
                        lambda payload, ts_ms=None: logged.append(payload) or "id1")
    monkeypatch.setattr(scan2.audit_log, "_load_entries", lambda path=None: [])
    monkeypatch.setattr(scan2, "CARDS_DIR", str(tmp_path))
    monkeypatch.setattr(scan2, "load_profile",
                        lambda: {"equity": 5000.0, "phase": 1})
    text = scan2.run_scan("manual", window_sec=0, coins=["HYPE"],
                          sleep=lambda s: None, assemble=fake_assemble,
                          notify=False)
    assert "HYPE" in text
    assert logged and logged[0]["mode"] == "SCAN"
    assert logged[0]["system"] == "scan2"
    # card file written
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].suffix == ".md"


def test_render_no_trade_lists_reasons():
    text = scan2.render({"cards": [], "no_trade_reasons": ["HYPE: no lean"]},
                        "manual", {"utc": "2026-07-26 12:55 UTC"})
    assert "NO-TRADE" in text and "no lean" in text


def test_flow_line_reads_coverage_pct_or_sample_pct():
    # production (this branch, off main): coverage_pct
    assert "sample 12%" in scan2._flow_line(
        {"delta_usdc": -500.0, "coverage_pct": 12.0})
    # fix/flow-honesty (unmerged): sample_pct — takes priority when present
    assert "sample 34%" in scan2._flow_line(
        {"delta_usdc": -500.0, "sample_pct": 34.0})


def test_coin_read_handles_non_dict_taker_delta():
    # fetch_market.assemble stores a string in taker_delta when
    # fetch_recent_trades raised DataUnavailable internally.
    snap_a = _snap(58.0, 100e6)
    snap_b = _snap(58.0 * 1.003, 101e6)
    snap_b["taker_delta"] = "DATA UNAVAILABLE: hyperliquid (recentTrades empty)"
    read = scan2._coin_read("HYPE", snap_a, snap_b)
    assert read["flow_line"] == "flow: DATA UNAVAILABLE"


def test_run_scan_isolates_one_failed_coin(tmp_path, monkeypatch):
    call_count = {}

    def fake_assemble(coin, deep=False, now_ms=None):
        call_count[coin] = call_count.get(coin, 0) + 1
        if coin == "ETH" and call_count[coin] == 2:   # fails on pull B
            raise RuntimeError("boom")
        return _snap(58.0, 100e6)

    monkeypatch.setattr(scan2.audit_log, "write_audit_entry",
                        lambda payload, ts_ms=None: "id1")
    monkeypatch.setattr(scan2.audit_log, "_load_entries", lambda path=None: [])
    monkeypatch.setattr(scan2, "CARDS_DIR", str(tmp_path))
    text = scan2.run_scan("manual", window_sec=0, coins=["HYPE", "ETH"],
                          sleep=lambda s: None, assemble=fake_assemble,
                          notify=False)
    assert "ETH: DATA UNAVAILABLE" in text
    assert "RuntimeError" in text
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].suffix == ".md"


def test_run_scan_raises_when_all_coins_fail(monkeypatch):
    def failing_assemble(coin, deep=False, now_ms=None):
        raise RuntimeError("boom")

    try:
        scan2.run_scan("manual", window_sec=0, coins=["HYPE"],
                       sleep=lambda s: None, assemble=failing_assemble,
                       notify=False)
        assert False, "expected DataUnavailable"
    except scan2.DataUnavailable as exc:
        assert "HYPE" in str(exc)


def test_log_entry_writes_scan2_entry_payload(monkeypatch):
    captured = []
    monkeypatch.setattr(
        scan2.audit_log, "write_audit_entry",
        lambda payload, ts_ms=None: captured.append(payload) or "tid-1")
    rc = scan2._cmd_log_entry(["HYPE", "long", "58.1", "57.5", "59.0", "60.0"])
    assert rc == 0
    assert captured[0]["mode"] == "ENTRY"
    assert captured[0]["system"] == "scan2"
    assert captured[0]["verdict"] == "TAKEN"
    assert captured[0]["side"] == "long"
    assert captured[0]["trigger"] == {"entry": 58.1, "stop": 57.5,
                                      "t1": 59.0, "t2": 60.0}


def test_log_entry_then_resolved_loss_feeds_session_stop(tmp_path,
                                                          monkeypatch):
    log_path = str(tmp_path / "audit.jsonl")
    monkeypatch.setattr(scan2.audit_log, "AUDIT_LOG_PATH", log_path)
    for _ in range(2):
        rc = scan2._cmd_log_entry(
            ["HYPE", "long", "58.0", "57.0", "59.0", "60.0"])
        assert rc == 0
    now_ms = int(time.time() * 1000)
    for e in scan2.audit_log._load_entries():
        scan2.audit_log.resolve_audit_entry(e["id"], -1.0, "stopped-out",
                                            resolved_at_ms=now_ms)
    entries = scan2.audit_log._load_entries()
    assert card2.session_stop_active(entries, now_ms)
