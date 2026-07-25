import json
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
                                    "sample_pct": 12.0}},
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
                        lambda payload: logged.append(payload) or "id1")
    monkeypatch.setattr(scan2.audit_log, "_load_entries", lambda path=None: [])
    monkeypatch.setattr(scan2, "CARDS_DIR", str(tmp_path))
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
