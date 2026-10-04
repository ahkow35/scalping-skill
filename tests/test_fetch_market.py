import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_market as fm


def _trade(time, side="B", tid=1):
    return {"time": time, "side": side, "tid": tid, "px": "100", "sz": "2"}


def test_old_cached_trade_cannot_create_coverage_for_empty_current_window():
    now = 10_000_000
    result = fm.bucket_taker_delta([_trade(now - 6 * 60_000)], now)["5m"]
    assert result["trade_count"] == 0
    assert result["coverage_pct"] is None
    assert result["capture_complete"] is None
    assert result["reliable"] is False
    assert result["sample_span_ms"] == 0
    assert result["sample_age_ms"] is None
    assert result["sample_fresh"] is False
    assert result["buy_share_pct"] is None


def test_rest_sample_span_is_not_capture_coverage_and_excludes_future_trades():
    now = 10_000_000
    trades = [_trade(now - 300_000), _trade(now - 10_000, "A", 2),
              _trade(now + 1, tid=3)]
    result = fm.bucket_taker_delta(trades, now)["5m"]
    assert result["trade_count"] == 2
    assert result["buy_usdc"] == 200
    assert result["sell_usdc"] == 200
    assert result["sample_span_ms"] == 290_000
    assert result["sample_span_pct"] == 96.7
    assert result["sample_age_ms"] == 10_000
    assert result["sample_fresh"] is True
    assert result["max_observed_gap_ms"] == 290_000
    assert result["observed_gap_count"] == 1
    assert result["coverage_pct"] is None
    assert result["reliable"] is False


def test_empty_and_stale_rest_windows_remain_unreliable():
    now = 10_000_000
    assert all(bucket["reliable"] is False for bucket in fm.bucket_taker_delta([], now).values())
    result = fm.bucket_taker_delta([_trade(now - 120_000)], now)["5m"]
    assert result["trade_count"] == 1
    assert result["sample_fresh"] is False
    assert result["sample_span_ms"] == 0
    assert result["coverage_pct"] is None


def test_trade_cache_discards_future_observations(tmp_path, monkeypatch):
    monkeypatch.setattr(fm, "TRADE_CACHE_DIR", str(tmp_path))
    now = 10_000_000
    valid = _trade(now - 1)
    assert fm.merge_trade_cache("HYPE", [valid, _trade(now + 1, tid=2)], now) == [valid]


def test_fetch_candles_preserves_exchange_time_and_marks_closed(monkeypatch):
    start = 1779102720000
    rows = [{"t": start, "T": start + 299_999, "o": "1", "h": "2",
             "l": "1", "c": "2", "v": "100"}]
    monkeypatch.setattr(fm, "_post_json", lambda *args: rows)
    forming = fm.fetch_candles("HYPE", "5m", start, start + 299_999)[0]
    assert forming["t"] == "2026-05-18 11:12 UTC"
    assert forming["t_ms"] == start
    assert forming["T"] == start + 299_999
    assert forming["closed"] is False
    assert fm.fetch_candles("HYPE", "5m", start, start + 300_000)[0]["closed"] is True


def test_iso_utc_is_utc_not_local():
    # 2026-05-18 11:12 UTC == 1779102720000 ms
    ms = 1779102720000
    assert fm.iso_utc(ms) == "2026-05-18 11:12 UTC"


def test_iso_utc_ignores_machine_timezone():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Singapore"
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
        assert fm.iso_utc(1779102720000) == "2026-05-18 11:12 UTC"
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()


def test_build_session_before_us_open():
    # 2026-05-18 11:12 UTC == 1779102720000 ms; ~2.3h before 13:30 UTC open
    s = fm.build_session(1779102720000)
    assert s["utc"] == "2026-05-18 11:12 UTC"
    assert s["sgt"] == "2026-05-18 19:12 SGT"
    assert round(s["hours_to_us_open"], 1) == 2.3
    assert s["us_session_live"] is False
    assert s["asia_handoff_soon"] is False


def test_build_session_during_us_session():
    # 2026-05-18 15:00 UTC == 1779116400000 ms (US session live, close in 5h)
    s = fm.build_session(1779116400000)
    assert s["us_session_live"] is True
    assert round(s["hours_to_us_close"], 1) == 5.0


def test_band_aggregate_buckets_usdc_depth():
    levels = [
        {"px": "45.98", "sz": "100"},
        {"px": "45.99", "sz": "50"},
        {"px": "46.03", "sz": "10"},
    ]
    out = fm.band_aggregate(levels, bucket=0.05)
    # nearest-bucket: 45.98/45.99 -> 46.0, 46.03 -> 46.05
    assert out[46.0] == 4598.0 + 2299.5
    assert out[46.05] == 460.3


def test_top_depth_sums_first_three_levels():
    levels = [
        {"px": "10.0", "sz": "1.0"},
        {"px": "11.0", "sz": "2.0"},
        {"px": "12.0", "sz": "3.0"},
        {"px": "13.0", "sz": "100.0"},
    ]
    out = fm.top_depth(levels, n=3)
    assert out == {"levels": 3, "sz": 6.0, "usdc": 68.0}


import pytest


def test_parse_btc_dominance_returns_only_current_snapshot():
    """CoinGecko /global does NOT expose historical BTC.D change. The 24h
    delta must come from the local cache, not the parser. (Previously this
    function mislabeled market_cap_change_percentage_24h_usd as
    btc_d_24h_chg, which is total-mcap change, not dominance change.)"""
    payload = {"data": {"market_cap_percentage": {"btc": 54.3},
                         "market_cap_change_percentage_24h_usd": 1.1}}
    out = fm.parse_btc_dominance(payload)
    assert out == {"btc_d": 54.3}
    assert "btc_d_24h_chg" not in out


def test_btcd_cache_appends_and_returns_sorted(tmp_path):
    p = tmp_path / "btcd.jsonl"
    now = 1779102720000
    rows1 = fm.update_btcd_cache(55.0, now, path=str(p))
    rows2 = fm.update_btcd_cache(55.5, now + 3600_000, path=str(p))
    assert len(rows2) == 2
    assert [r["btc_d"] for r in rows2] == [55.0, 55.5]
    assert rows2[0]["ts"] < rows2[1]["ts"]


def test_btcd_cache_drops_old_rows(tmp_path):
    p = tmp_path / "btcd.jsonl"
    now = 1779102720000
    # Seed a 49h-old row directly
    p.write_text(json.dumps({"ts": now - 49 * 3600_000, "btc_d": 50.0}) + "\n")
    rows = fm.update_btcd_cache(55.0, now, path=str(p))
    assert len(rows) == 1
    assert rows[0]["btc_d"] == 55.0


def test_btcd_24h_change_picks_sample_in_tolerance_window():
    now = 1779102720000
    rows = [
        {"ts": now - 25 * 3600_000, "btc_d": 56.0},   # 25h ago — in window
        {"ts": now - 12 * 3600_000, "btc_d": 55.5},   # 12h ago — out of window
        {"ts": now, "btc_d": 54.5},
    ]
    out = fm.compute_btcd_24h_change(rows, now, current_btcd=54.5)
    assert out["btc_d_24h_chg"] == round((54.5 - 56.0) / 56.0 * 100, 3)
    assert out["btc_d_sample_age_min"] == 25 * 60
    assert out["btc_d_coverage_h"] == 25.0


def test_btcd_24h_change_none_when_no_sample_near_24h():
    now = 1779102720000
    rows = [
        {"ts": now - 6 * 3600_000, "btc_d": 55.0},   # only 6h of history
        {"ts": now, "btc_d": 55.2},
    ]
    out = fm.compute_btcd_24h_change(rows, now, current_btcd=55.2)
    assert out["btc_d_24h_chg"] is None
    assert out["btc_d_sample_age_min"] is None
    assert out["btc_d_coverage_h"] == 6.0


def test_btcd_24h_change_tolerance_bounds():
    now = 1779102720000
    # Sample exactly at 22.4h ago — just OUTSIDE the lower tolerance bound (22.5h)
    rows = [
        {"ts": now - int(22.4 * 3600_000), "btc_d": 55.0},
        {"ts": now, "btc_d": 55.2},
    ]
    out = fm.compute_btcd_24h_change(rows, now, current_btcd=55.2)
    assert out["btc_d_24h_chg"] is None

    # Sample exactly at 22.6h ago — inside the window
    rows = [
        {"ts": now - int(22.6 * 3600_000), "btc_d": 55.0},
        {"ts": now, "btc_d": 55.2},
    ]
    out = fm.compute_btcd_24h_change(rows, now, current_btcd=55.2)
    assert out["btc_d_24h_chg"] is not None


def test_btcd_24h_change_picks_closest_sample_when_multiple_in_window():
    now = 1779102720000
    rows = [
        {"ts": now - int(23.0 * 3600_000), "btc_d": 56.0},   # 1h from target
        {"ts": now - int(24.5 * 3600_000), "btc_d": 56.5},   # 0.5h from target — closer
        {"ts": now - int(25.4 * 3600_000), "btc_d": 57.0},   # 1.4h from target
        {"ts": now, "btc_d": 55.0},
    ]
    out = fm.compute_btcd_24h_change(rows, now, current_btcd=55.0)
    # Should have picked the 56.5 sample (closest to 24h ago)
    expected = round((55.0 - 56.5) / 56.5 * 100, 3)
    assert out["btc_d_24h_chg"] == expected


def test_btcd_24h_change_handles_empty_cache():
    now = 1779102720000
    out = fm.compute_btcd_24h_change([], now, current_btcd=55.0)
    assert out["btc_d_24h_chg"] is None
    assert out["btc_d_sample_age_min"] is None
    assert out["btc_d_coverage_h"] is None


def test_data_unavailable_raises_named_error():
    with pytest.raises(fm.DataUnavailable) as e:
        fm.parse_btc_dominance({"unexpected": True})
    assert "coingecko" in str(e.value).lower()


def test_assemble_marks_macro_unavailable_when_btcd_fails(monkeypatch):
    monkeypatch.setattr(fm, "fetch_core_meta", lambda: [
        {"universe": [{"name": "HYPE"}, {"name": "BTC"}]},
        [
            {"markPx": "46.0", "oraclePx": "46.0", "midPx": "46.0",
             "funding": "0", "premium": "0", "openInterest": "0",
             "prevDayPx": "46.0", "dayNtlVlm": "0"},
            {"markPx": "100000.0", "oraclePx": "100000.0", "midPx": "100000.0",
             "funding": "0", "premium": "0", "openInterest": "0",
             "prevDayPx": "100000.0", "dayNtlVlm": "0"},
        ],
    ])
    monkeypatch.setattr(fm, "fetch_candles", lambda *a: [])
    monkeypatch.setattr(fm, "fetch_l2", lambda c: {"asks": {}, "bids": {}})
    # Isolate the taker-delta path too: without these, the test makes a live
    # recentTrades call AND writes real trades into .trade_cache/.
    monkeypatch.setattr(fm, "fetch_recent_trades", lambda coin: [])
    monkeypatch.setattr(fm, "merge_trade_cache", lambda c, f, n: [])
    monkeypatch.setattr(fm, "update_oi_cache", lambda *a, **k: [])

    def boom():
        raise fm.DataUnavailable("DATA UNAVAILABLE: coingecko (down)")
    monkeypatch.setattr(fm, "fetch_btc_dominance", boom)

    # Isolate from the real on-disk BTC.D cache: with the live fetch down, the
    # fallback reads the cache; force that to fail too so this test deterministically
    # exercises the fully-unavailable path (not whatever snapshot is on disk).
    def no_cache(*a, **k):
        raise fm.DataUnavailable("DATA UNAVAILABLE: coingecko (no cache)")
    monkeypatch.setattr(fm, "_read_latest_btcd", no_cache)

    out = fm.assemble("HYPE", deep=False, now_ms=1779102720000)
    assert out["btc_dominance"] == "DATA UNAVAILABLE: coingecko (down)"
    assert out["macro_can_clear"] is False


def test_weekend_window_true_friday_evening():
    # 2026-05-22 is a Friday. 21:00 UTC == inside the weekend window (Fri 20:00 → Sun 20:00).
    # 2026-05-22 21:00 UTC = 1779483600000 ms
    s = fm.build_session(1779483600000)
    assert s["weekend_window"] is True


def test_weekend_window_true_saturday():
    # 2026-05-23 is a Saturday. 12:00 UTC == inside the window.
    # 2026-05-23 12:00 UTC = 1779537600000 ms
    s = fm.build_session(1779537600000)
    assert s["weekend_window"] is True


def test_weekend_window_false_friday_morning():
    # 2026-05-22 (Fri) 10:00 UTC == before the 20:00 Fri start.
    # 2026-05-22 10:00 UTC = 1779444000000 ms
    s = fm.build_session(1779444000000)
    assert s["weekend_window"] is False


def test_weekend_window_false_sunday_late():
    # 2026-05-24 (Sun) 21:00 UTC == after the 20:00 Sun end.
    # 2026-05-24 21:00 UTC = 1779656400000 ms
    s = fm.build_session(1779656400000)
    assert s["weekend_window"] is False


# --- microprice (Stoikov 2018, top-of-book simple form) -----------------------

def test_microprice_empty_book_returns_none():
    out = fm.compute_microprice([], [])
    assert out["microprice"] is None
    assert out["mid_l1"] is None
    assert out["note"] == "empty book"


def test_microprice_balanced_sizes_equals_mid():
    # Equal size on both sides => microprice == mid; dev = 0 bps.
    bids = [{"px": "42.00", "sz": "100"}]
    asks = [{"px": "42.10", "sz": "100"}]
    out = fm.compute_microprice(bids, asks)
    assert out["mid_l1"] == 42.05
    assert out["microprice"] == 42.05
    assert out["microprice_dev_bps"] == 0.0


def test_microprice_bid_heavy_leans_toward_ask():
    # 10x bid stack => fair value pulled toward ask. Canonical Stoikov example.
    # micro = 42.10 * (1000/1100) + 42.00 * (100/1100) = 42.090909...
    bids = [{"px": "42.00", "sz": "1000"}]
    asks = [{"px": "42.10", "sz": "100"}]
    out = fm.compute_microprice(bids, asks)
    assert abs(out["microprice"] - 42.0909) < 1e-3
    assert out["microprice_dev_bps"] > 0


def test_microprice_ask_heavy_leans_toward_bid():
    # micro = 42.10 * (100/1100) + 42.00 * (1000/1100) = 42.009090...
    bids = [{"px": "42.00", "sz": "100"}]
    asks = [{"px": "42.10", "sz": "1000"}]
    out = fm.compute_microprice(bids, asks)
    assert abs(out["microprice"] - 42.0091) < 1e-3
    assert out["microprice_dev_bps"] < 0


def test_microprice_zero_size_returns_none():
    bids = [{"px": "42.00", "sz": "0"}]
    asks = [{"px": "42.10", "sz": "0"}]
    out = fm.compute_microprice(bids, asks)
    assert out["microprice"] is None
    assert "zero size" in out["note"]


def test_microprice_malformed_levels_returns_none():
    out = fm.compute_microprice([{"px": "bad"}], [{"px": "42.10", "sz": "1"}])
    assert out["microprice"] is None
    assert "malformed" in out["note"]


def test_microprice_crossed_book_is_flagged_but_computed():
    # Bid above ask — pathological but still produces a number; note must flag it.
    bids = [{"px": "42.20", "sz": "100"}]
    asks = [{"px": "42.10", "sz": "100"}]
    out = fm.compute_microprice(bids, asks)
    assert out["microprice"] is not None
    assert "crossed" in out["note"]


def test_microprice_carries_taker_fee_reference():
    bids = [{"px": "42.00", "sz": "100"}]
    asks = [{"px": "42.10", "sz": "100"}]
    out = fm.compute_microprice(bids, asks)
    assert out["taker_fee_bps_reference"] == fm.HL_TAKER_FEE_BPS


def test_fetch_l2_emits_execution_key(monkeypatch):
    # Verify the integration point: fetch_l2 still returns asks/bids AND now execution.
    def fake_post(url, payload, source):
        return {"levels": [
            [{"px": "42.00", "sz": "100"}],  # bids
            [{"px": "42.10", "sz": "100"}],  # asks
        ]}
    monkeypatch.setattr(fm, "_post_json", fake_post)
    out = fm.fetch_l2("HYPE")
    assert "asks" in out and "bids" in out  # back-compat
    assert "execution" in out
    assert out["execution"]["microprice"] == 42.05
    assert out["execution"]["microprice_dev_bps"] == 0.0


# --- HIP-3 namespacing -------------------------------------------------------

def test_parse_coin_arg_core_perp_uppercases():
    coin, dex = fm.parse_coin_arg("hype")
    assert coin == "HYPE" and dex is None


def test_parse_coin_arg_hip3_preserves_dex_lowercase_base_upper():
    coin, dex = fm.parse_coin_arg("xyz:SPCX")
    assert coin == "xyz:SPCX" and dex == "xyz"


def test_parse_coin_arg_hip3_normalizes_mixed_case():
    coin, dex = fm.parse_coin_arg("XYZ:spcx")
    assert coin == "xyz:SPCX" and dex == "xyz"


def test_trade_cache_path_sanitizes_colon():
    p = fm._trade_cache_path("xyz:SPCX")
    assert p.endswith("xyz_SPCX.jsonl")
    assert ":" not in p.split("/")[-1]


def test_fetch_ctx_passes_dex_param_for_hip3(monkeypatch):
    seen = {}

    def fake_post(url, payload, source):
        seen["payload"] = payload
        return [
            {"universe": [{"name": "xyz:SPCX"}]},
            [{
                "markPx": "200.0", "oraclePx": "200.5", "midPx": "199.9",
                "funding": "0.0000001", "premium": "-0.005",
                "openInterest": "1000", "prevDayPx": "198.0",
                "dayNtlVlm": "5000000",
            }],
        ]

    monkeypatch.setattr(fm, "_post_json", fake_post)
    out = fm.fetch_ctx("xyz:SPCX", dex="xyz")
    assert seen["payload"] == {"type": "metaAndAssetCtxs", "dex": "xyz"}
    assert out["coin"] == "xyz:SPCX"
    assert out["mark"] == 200.0


def test_fetch_ctx_omits_dex_for_core_perps(monkeypatch):
    seen = {}

    def fake_post(url, payload, source):
        seen["payload"] = payload
        return [
            {"universe": [{"name": "HYPE"}]},
            [{
                "markPx": "73.5", "oraclePx": "73.4", "midPx": "73.49",
                "funding": "0.00001", "premium": "0.001",
                "openInterest": "20000000", "prevDayPx": "67.0",
                "dayNtlVlm": "1300000000",
            }],
        ]

    monkeypatch.setattr(fm, "_post_json", fake_post)
    out = fm.fetch_ctx("HYPE")
    assert seen["payload"] == {"type": "metaAndAssetCtxs"}  # no dex key
    assert out["coin"] == "HYPE"


def test_fetch_ctx_error_message_distinguishes_hip3_vs_core():
    # Core not-found
    with pytest.raises(fm.DataUnavailable) as e:
        fm.parse_btc_dominance({})  # warm up pytest import
    # Real test: simulate missing coin and confirm the dex hint appears.
    import unittest.mock as um
    with um.patch.object(fm, "_post_json", return_value=[{"universe": [{"name": "OTHER"}]}, []]):
        with pytest.raises(fm.DataUnavailable) as e:
            fm.fetch_ctx("xyz:NOPE", dex="xyz")
        assert "dex='xyz'" in str(e.value)
        with pytest.raises(fm.DataUnavailable) as e:
            fm.fetch_ctx("NOPE")
        assert "core perps" in str(e.value)


def test_assemble_attaches_regime_block(monkeypatch):
    import fetch_market as fm

    def fake_series(closes):
        return [{"t": "x", "o": x, "h": x + 0.5, "l": x - 0.5, "c": x, "v": 1.0}
                for x in closes]

    osc = fake_series([100, 101, 100, 101, 100, 101] * 8)

    monkeypatch.setattr(fm, "fetch_core_meta", lambda: [
        {"universe": [{"name": "HYPE"}, {"name": "BTC"}]},
        [
            {"markPx": "100.0", "oraclePx": "100.0", "midPx": "100.0",
             "funding": "0", "premium": "0", "openInterest": "0",
             "prevDayPx": "100.0", "dayNtlVlm": "0"},
            {"markPx": "100.0", "oraclePx": "100.0", "midPx": "100.0",
             "funding": "0", "premium": "0", "openInterest": "0",
             "prevDayPx": "100.0", "dayNtlVlm": "0"},
        ],
    ])
    monkeypatch.setattr(fm, "fetch_candles",
                        lambda coin, interval, s, e: osc)
    monkeypatch.setattr(fm, "fetch_l2", lambda coin: {
        "asks": {}, "bids": {},
        "execution": {"microprice_dev_bps": 0.1, "microprice": 100.0}})
    monkeypatch.setattr(fm, "fetch_recent_trades", lambda coin: [])
    monkeypatch.setattr(fm, "merge_trade_cache", lambda c, f, n: [])
    monkeypatch.setattr(fm, "bucket_taker_delta", lambda t, n: {
        "5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0},
        "1h": {"buy_share_pct": 50.0}})
    monkeypatch.setattr(fm, "fetch_btc_dominance", lambda: {"btc_d": 55.0})
    monkeypatch.setattr(fm, "update_btcd_cache", lambda d, n: [])
    monkeypatch.setattr(fm, "compute_btcd_24h_change", lambda r, n, d: {
        "btc_d_24h_chg": None, "btc_d_sample_age_min": None, "btc_d_coverage_h": 0.0})
    monkeypatch.setattr(fm, "update_oi_cache", lambda *a, **k: [])

    out = fm.assemble("HYPE", now_ms=1_750_000_000_000)
    assert "regime" in out
    assert out["regime"]["regime_label"] in {
        "trending", "ranging", "quiet", "correlated-chop", "unknown"}
    assert "15m" in out["btc_candles"]
    # VWAP + OI blocks are always present (READ-ONLY Phase 1 context)
    assert "vwap" in out
    assert out["oi"]["1h"]["read"] is None  # empty cache = warming
    assert out["oi"]["coverage_h"] == 0.0


# ── VWAP (UTC-day anchored) ─────────────────────────────────────────────

DAY_MS = 86_400_000
_MIDNIGHT = (1_750_000_000_000 // DAY_MS) * DAY_MS  # a UTC midnight
HOUR_MS = 3_600_000


def _bar(ts_ms, px, vol):
    return {"t": fm.iso_utc(ts_ms), "o": px, "h": px, "l": px, "c": px,
            "v": vol}


def test_compute_vwap_excludes_bars_before_utc_midnight():
    candles = [
        _bar(_MIDNIGHT - HOUR_MS, 999.0, 100.0),   # yesterday — excluded
        _bar(_MIDNIGHT, 10.0, 1.0),
        _bar(_MIDNIGHT + HOUR_MS, 20.0, 1.0),
    ]
    out = fm.compute_vwap(candles, _MIDNIGHT + 2 * HOUR_MS)
    assert out["bars"] == 2
    assert out["vwap"] == pytest.approx(15.0)


def test_compute_vwap_is_volume_weighted_hlc3():
    candles = [
        {"t": fm.iso_utc(_MIDNIGHT), "o": 0.0, "h": 12.0, "l": 9.0, "c": 9.0,
         "v": 3.0},   # hlc3 = 10, weight 3
        {"t": fm.iso_utc(_MIDNIGHT + HOUR_MS), "o": 0.0, "h": 21.0, "l": 19.0,
         "c": 20.0, "v": 1.0},  # hlc3 = 20, weight 1
    ]
    out = fm.compute_vwap(candles, _MIDNIGHT + 2 * HOUR_MS)
    assert out["vwap"] == pytest.approx((10.0 * 3 + 20.0 * 1) / 4)


def test_compute_vwap_none_on_zero_volume_or_empty():
    assert fm.compute_vwap([], _MIDNIGHT) is None
    assert fm.compute_vwap([_bar(_MIDNIGHT, 10.0, 0.0)], _MIDNIGHT + 1) is None


# ── OI cache + OI×price read ────────────────────────────────────────────


def test_update_oi_cache_appends_prunes_and_filters_by_coin(tmp_path):
    path = str(tmp_path / "oi.jsonl")
    now = _MIDNIGHT
    fm.update_oi_cache("HYPE", 100.0, 10.0, now - 49 * HOUR_MS, path=path)
    fm.update_oi_cache("BTC", 555.0, 90.0, now - HOUR_MS, path=path)
    rows = fm.update_oi_cache("HYPE", 120.0, 11.0, now, path=path)
    # 49h-old row pruned; BTC row filtered out of the return
    assert [r["oi_usdc"] for r in rows] == [120.0]
    with open(path) as f:
        on_disk = [line for line in f if line.strip()]
    assert len(on_disk) == 2  # BTC + fresh HYPE survive on disk


def test_compute_oi_change_uses_aligned_sample_and_tolerance():
    now = _MIDNIGHT
    rows = [{"ts": now - HOUR_MS - 5 * 60_000, "coin": "HYPE",
             "oi_usdc": 100.0, "mark": 10.0}]
    out = fm.compute_oi_change(rows, now, 110.0, 10.5, HOUR_MS, 15 * 60_000)
    assert out["oi_chg_pct"] == pytest.approx(10.0)
    assert out["price_chg_pct"] == pytest.approx(5.0)
    # outside tolerance → warming
    far = fm.compute_oi_change(rows, now, 110.0, 10.5, HOUR_MS, 60_000)
    assert far == {"oi_chg_pct": None, "price_chg_pct": None,
                   "sample_age_min": None}


def test_classify_oi_read_matrix():
    assert fm.classify_oi_read(1.0, 2.0) == "new-longs"
    assert fm.classify_oi_read(1.0, -2.0) == "short-covering"
    assert fm.classify_oi_read(-1.0, 2.0) == "new-shorts"
    assert fm.classify_oi_read(-1.0, -2.0) == "long-unwind"
    assert fm.classify_oi_read(0.1, 5.0) == "flat"    # price inside noise
    assert fm.classify_oi_read(1.0, 0.2) == "flat"    # OI inside noise
    assert fm.classify_oi_read(None, 2.0) is None     # cache warming


# ── recorder tape → taker_delta buckets (source ws_recorder) ──────────────

MIN = 60_000
TAPE_NOW = (1_750_000_000_000 // MIN) * MIN + 30_000   # 30 s into a minute
TAPE_M = TAPE_NOW - 30_000                              # the current minute's start


def _tape(**over):
    """A healthy 4h tape: 10 trades / 100 buy / 50 sell every minute."""
    start = TAPE_NOW - 240 * MIN
    first_minute = -(-start // MIN) * MIN
    tape = {"coin": "HYPE", "now_ms": TAPE_NOW, "window_start_ms": start, "connected": True,
            "first_trade_ms": first_minute + 1_000, "last_trade_ms": TAPE_NOW - 5_000,
            "rows": [{"t_ms": t, "buy_usdc": 100.0, "sell_usdc": 50.0, "count": 10}
                     for t in range(first_minute, TAPE_M + 1, MIN)],
            "gaps": []}
    tape.update(over)
    return tape


def _candles(n=10, skip=(), none_at=()):
    return [{"t_ms": t, "n": None if t in none_at else n}
            for t in range(TAPE_M - 241 * MIN, TAPE_M + 1, MIN) if t not in skip]


def _five(tape, candles):
    return fm.bucket_taker_delta_tape(tape, candles)["5m"]


def test_tape_bucket_good_window_is_reliable_with_exact_coverage_and_rest_keys():
    tape_b = fm.bucket_taker_delta_tape(_tape(), _candles())
    rest_b = fm.bucket_taker_delta([], TAPE_NOW)
    five = tape_b["5m"]
    assert set(five) == set(rest_b["5m"])
    assert five["source"] == "ws_recorder"
    assert five["coverage_pct"] == 100.0
    assert five["capture_complete"] is True and five["reliable"] is True
    assert five["sample_age_ms"] == 5_000
    # whole minutes from m-4 through the current partial minute m: 5 rows
    assert (five["trade_count"], five["buy_usdc"], five["sell_usdc"]) == (50, 500.0, 250.0)
    assert five["delta_usdc"] == 250.0 and five["buy_share_pct"] == 66.7
    assert tape_b["4h"]["reliable"] is False      # no candle counts beyond the hour


def test_tape_coverage_maths_recorded_over_exchange_trade_count():
    assert _five(_tape(), _candles(n=20))["coverage_pct"] == 50.0
    assert _five(_tape(), _candles(n=20))["reliable"] is True      # exactly the 50 floor
    low = _five(_tape(), _candles(n=25))
    assert low["coverage_pct"] == 40.0 and low["reliable"] is False
    assert low["capture_complete"] is True                          # complete != covered enough
    assert _five(_tape(), _candles(n=5))["coverage_pct"] == 100.0   # capped, never above 100


def test_tape_gap_inside_the_window_means_not_complete_and_not_reliable():
    gap = {"reconnect_ts_ms": TAPE_NOW - 2 * MIN, "last_seen_exchange_ts_ms": TAPE_NOW - 3 * MIN}
    tape_b = fm.bucket_taker_delta_tape(_tape(gaps=[gap]), _candles())
    assert tape_b["5m"]["capture_complete"] is False and tape_b["5m"]["reliable"] is False
    assert tape_b["5m"]["observed_gap_count"] == 1
    assert tape_b["4h"]["capture_complete"] is None
    old_gap = {"reconnect_ts_ms": TAPE_NOW - 30 * MIN, "last_seen_exchange_ts_ms": None}
    tape_b = fm.bucket_taker_delta_tape(_tape(gaps=[old_gap]), _candles())
    assert tape_b["5m"]["reliable"] is True and tape_b["15m"]["reliable"] is True   # gap is older
    assert tape_b["1h"]["capture_complete"] is False


def test_tape_not_connected_is_not_complete():
    five = _five(_tape(connected=False), _candles())
    assert five["capture_complete"] is False and five["reliable"] is False


def test_tape_stale_last_trade_is_not_reliable():
    five = _five(_tape(last_trade_ms=TAPE_NOW - 61_000), _candles())
    assert five["sample_age_ms"] == 61_000 and five["sample_fresh"] is False
    assert five["reliable"] is False
    assert _five(_tape(last_trade_ms=TAPE_NOW - 60_000), _candles())["reliable"] is True


def test_tape_missing_candle_count_gives_null_coverage_and_not_reliable():
    for candles in (_candles(none_at={TAPE_M - 2 * MIN}), _candles(skip={TAPE_M - 2 * MIN})):
        five = _five(_tape(), candles)
        assert five["coverage_pct"] is None
        assert five["capture_complete"] is None and five["reliable"] is False


def test_tape_with_no_trades_stays_null_not_zero():
    five = _five(_tape(rows=[], first_trade_ms=None, last_trade_ms=None), _candles())
    assert five["buy_share_pct"] is None and five["coverage_pct"] is None
    assert five["sample_age_ms"] is None and five["reliable"] is False


def test_tape_fallback_reason_names_the_first_failing_condition():
    def reason(tape, candles):
        return fm.tape_fallback_reason(fm.bucket_taker_delta_tape(tape, candles), tape)

    assert reason(_tape(), _candles()) is None
    assert reason(_tape(last_trade_ms=TAPE_NOW - 90_000), _candles()) == "stale"
    assert reason(_tape(connected=False), _candles()) == "recorder disconnected"
    gap = {"reconnect_ts_ms": TAPE_NOW - MIN, "last_seen_exchange_ts_ms": None}
    assert reason(_tape(gaps=[gap]), _candles()) == "gap"
    assert reason(_tape(), _candles(none_at={TAPE_M - MIN})) == "candle counts missing"
    assert reason(_tape(), _candles(n=100)) == "coverage low"


def _candles_fn(rows=None, calls=None):
    def fn(coin, iv, start, end):
        if calls is not None:
            calls.append((iv, start, end))
        return [c for c in (rows or _candles()) if start <= c["t_ms"] <= end]
    return fn


def test_fetch_tape_taker_delta_uses_the_tape_only_when_the_5m_window_is_reliable():
    buckets, why = fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (_tape(), None),
                                             candles=_candles_fn(), clock=lambda: TAPE_NOW)
    assert why is None and buckets["5m"]["reliable"] is True
    buckets, why = fm.fetch_tape_taker_delta("HYPE", candles=_candles_fn(), clock=lambda: TAPE_NOW,
                                             fetch_flow=lambda c: (_tape(connected=False), None))
    assert buckets is None and why == "recorder disconnected"


def test_tape_is_measured_on_the_recorders_clock_not_the_callers():
    # assemble() reads its clock before ~9 exchange calls; on a busy coin the
    # tape's last trade is then "after" that old clock. Must still be reliable.
    for behind in (1_500, 4_000):
        tape = _tape(last_trade_ms=TAPE_NOW - 200)
        buckets, why = fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c, t=tape: (t, None),
                                                 candles=_candles_fn(), clock=lambda b=behind: TAPE_NOW - b)
        assert why is None, why
        five = buckets["5m"]
        assert five["reliable"] is True and five["sample_age_ms"] == 200 and five["sample_fresh"] is True
        assert five["trade_count"] == 50


def test_a_tape_body_older_than_60s_or_clocks_far_apart_is_stale():
    def run(clock_offset):
        return fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (_tape(), None),
                                         candles=_candles_fn(), clock=lambda: TAPE_NOW + clock_offset)

    # _tape's last trade is 5 s before the tape's clock, so on our clock it is
    # 60 s old at +55 s: the freshness rule holds on whichever clock is later.
    assert run(55_000)[1] is None            # exactly 60 s old on our clock still passes
    assert run(-60_000)[1] is None           # our clock behind by exactly the tolerance
    for offset in (56_000, 61_000, 300_000, -61_000):
        assert run(offset) == (None, "stale")


def test_tape_candles_are_the_trailing_hour_in_chunks_of_at_most_50_bars():
    calls = []
    buckets, why = fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (_tape(), None),
                                             candles=_candles_fn(calls=calls), clock=lambda: TAPE_NOW)
    assert why is None
    assert len(calls) == 2 and all(iv == "1m" for iv, _, _ in calls)
    assert all((end - start + 1) <= 50 * MIN for _, start, end in calls)
    assert calls[0][1] == TAPE_M - 60 * MIN and calls[-1][2] == TAPE_NOW
    # the 4h window keeps its sums but has no coverage verdict
    four = buckets["4h"]
    assert four["trade_count"] > buckets["1h"]["trade_count"]
    assert four["coverage_pct"] is None and four["capture_complete"] is None
    assert four["reliable"] is False and "not computed" in four["note"]
    assert "note" not in buckets["5m"]
    assert buckets["1h"]["coverage_pct"] == 100.0 and buckets["1h"]["reliable"] is True


def test_fetch_tape_taker_delta_falls_back_with_a_distinct_reason():
    for answer in ((None, "unreachable"), (None, "coin not recorded"), (None, "watcher not configured"),
                   (None, "unauthorized"), (None, "recorder not configured")):
        assert fm.fetch_tape_taker_delta("DOGE", fetch_flow=lambda c, a=answer: a,
                                         candles=_candles_fn()) == answer

    def boom(*a):
        raise fm.DataUnavailable("candles down")

    kw = dict(clock=lambda: TAPE_NOW)
    assert fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (_tape(), None),
                                     candles=boom, **kw) == (None, "candles unavailable")
    for garbage in ({"rows": "garbage"}, {"now_ms": "x"}, _tape(rows=[{"t_ms": TAPE_M}]),
                    _tape(last_trade_ms=TAPE_NOW + 5_000)):
        assert fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c, g=garbage: (g, None),
                                         candles=_candles_fn(), **kw) == (None, "malformed tape")


def test_tape_thresholds_come_from_flow_params_not_copies():
    assert not hasattr(fm, "TAPE_MIN_COVERAGE_PCT") and not hasattr(fm, "TAPE_MAX_AGE_MS")
    import flow
    stricter = {**flow.DEFAULT_PARAMS, "min_coverage": 70.0}
    assert _five(_tape(), _candles(n=15))["coverage_pct"] == 66.7
    assert _five(_tape(), _candles(n=15))["reliable"] is True
    flow_params = flow.DEFAULT_PARAMS
    try:
        flow.DEFAULT_PARAMS = stricter
        assert _five(_tape(), _candles(n=15))["reliable"] is False
    finally:
        flow.DEFAULT_PARAMS = flow_params


def test_a_recorder_that_started_ten_minutes_ago_is_reliable_for_5m_only(tmp_path):
    import recorder as rec

    started = TAPE_NOW - 10 * MIN
    records = [rec.make_gap_record("HYPE", "trades", None, started)]   # cold-start gap record
    tid = 0
    for t in range(-(-started // MIN) * MIN, TAPE_M + 1, MIN):
        for k in range(10):
            tid += 1
            ms = t + 1_000 + k * 1_000
            if ms <= TAPE_NOW - 5_000:
                records.append(rec.make_data_record(
                    "HYPE", "trades", {"coin": "HYPE", "side": "B" if k % 2 else "A", "px": "10",
                                       "sz": "1", "time": ms, "tid": tid}, ms, ms + 5))
    path = rec.file_path(str(tmp_path), "HYPE", "trades", rec.day_str_utc(TAPE_NOW))
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    tape = rec.read_flow(str(tmp_path), "HYPE", connected=True, now=TAPE_NOW)
    ns = {}
    for r in tape["rows"]:
        ns[r["t_ms"]] = r["count"]
    candles = [{"t_ms": t, "n": ns.get(t, 10)} for t in range(TAPE_M - 61 * MIN, TAPE_M + 1, MIN)]
    buckets, why = fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (tape, None),
                                             candles=_candles_fn(candles), clock=lambda: TAPE_NOW)
    assert why is None
    assert buckets["5m"]["reliable"] is True
    for window in ("15m", "1h", "4h"):
        assert buckets[window]["reliable"] is False
    assert buckets["15m"]["observed_gap_count"] == 1 and buckets["15m"]["capture_complete"] is False


def test_bucket_taker_delta_rest_is_still_unreliable_with_the_rest_source():
    trades = [_trade(TAPE_NOW - 1_000 * i, "B" if i % 2 else "A", i) for i in range(1, 30)]
    for window in fm.bucket_taker_delta(trades, TAPE_NOW).values():
        assert window["source"] == "recent_trades_rest"
        assert window["reliable"] is False
        assert window["coverage_pct"] is None and window["capture_complete"] is None


def _stub_assemble(monkeypatch):
    monkeypatch.setattr(fm, "fetch_core_meta", lambda: [
        {"universe": [{"name": "HYPE"}, {"name": "BTC"}]},
        [{"markPx": "46", "oraclePx": "46", "midPx": "46", "funding": "0", "premium": "0",
          "openInterest": "0", "prevDayPx": "46", "dayNtlVlm": "0"},
         {"markPx": "1", "oraclePx": "1", "midPx": "1", "funding": "0", "premium": "0",
          "openInterest": "0", "prevDayPx": "1", "dayNtlVlm": "0"}]])
    monkeypatch.setattr(fm, "fetch_candles", lambda *a: [])
    monkeypatch.setattr(fm, "fetch_l2", lambda c: {"asks": {}, "bids": {}})
    monkeypatch.setattr(fm, "fetch_recent_trades", lambda coin: [_trade(TAPE_NOW - 1_000)])
    monkeypatch.setattr(fm, "merge_trade_cache", lambda c, f, n: f)
    monkeypatch.setattr(fm, "update_oi_cache", lambda *a, **k: [])
    monkeypatch.setattr(fm, "fetch_btc_dominance",
                        lambda: (_ for _ in ()).throw(fm.DataUnavailable("DATA UNAVAILABLE: x")))
    monkeypatch.setattr(fm, "_read_latest_btcd",
                        lambda *a, **k: (_ for _ in ()).throw(fm.DataUnavailable("DATA UNAVAILABLE: x")))


def _stub_tape(monkeypatch, tape_answer):
    monkeypatch.setattr(fm, "fetch_tape_taker_delta", lambda coin, **kw: tape_answer)


def test_assemble_falls_back_to_rest_with_reliable_false_and_says_why(monkeypatch):
    for reason in ("unreachable", "coin not recorded", "gap"):
        _stub_assemble(monkeypatch)
        _stub_tape(monkeypatch, (None, reason))
        out = fm.assemble("HYPE", now_ms=TAPE_NOW, use_tape=True)
        assert out["taker_delta_source"] == {"used": "recent_trades_rest", "fallback_reason": reason}
        assert out["taker_delta"]["5m"]["source"] == "recent_trades_rest"
        assert out["taker_delta"]["5m"]["reliable"] is False
        assert out["taker_delta"]["5m"]["coverage_pct"] is None
        assert out["flow"]["coverage_ok"] is False


def test_assemble_uses_a_reliable_tape_and_the_flow_gate_accepts_it(monkeypatch):
    buckets = fm.bucket_taker_delta_tape(_tape(), _candles())
    _stub_assemble(monkeypatch)
    _stub_tape(monkeypatch, (buckets, None))
    out = fm.assemble("HYPE", now_ms=TAPE_NOW, use_tape=True)
    assert out["taker_delta_source"] == {"used": "ws_recorder", "fallback_reason": None}
    assert out["taker_delta"]["5m"]["source"] == "ws_recorder"
    assert out["flow"]["coverage_ok"] is True
    assert out["flow"]["aggressor_bias"] == "buyers"     # 66.7% buy share on a complete tape


def test_assemble_without_use_tape_never_asks_for_the_tape(monkeypatch):
    import account_monitor
    _stub_assemble(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("remote_flow must not be called")

    monkeypatch.setattr(account_monitor, "remote_flow", boom)
    monkeypatch.setattr(fm, "fetch_tape_taker_delta", boom)
    out = fm.assemble("HYPE", now_ms=TAPE_NOW)
    assert "taker_delta_source" not in out
    assert out["taker_delta"]["5m"]["source"] == "recent_trades_rest"


def test_main_asks_for_the_tape(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(fm, "assemble", lambda coin, deep=False, now_ms=None, use_tape=False:
                        seen.update(use_tape=use_tape) or {"primary": coin})
    monkeypatch.setattr(fm, "snapshot_market", lambda out: None)
    assert fm.main(["fetch_market.py", "HYPE"]) == 0
    assert seen["use_tape"] is True


def test_assemble_with_the_tape_stubs_only_the_network_boundaries(monkeypatch):
    """No wholesale stub of fetch_tape_taker_delta: the caller's clock was read
    long before the tape arrived and sits 4 s behind the recorder's."""
    import account_monitor
    _stub_assemble(monkeypatch)
    tape = _tape(last_trade_ms=TAPE_NOW - 100)
    monkeypatch.setattr(account_monitor, "remote_flow", lambda coin, *a, **k: (tape, None))
    monkeypatch.setattr(fm, "_NOW", lambda: TAPE_NOW - 4_000)
    monkeypatch.setattr(fm, "fetch_candles",
                        lambda coin, iv, s, e: [c for c in _candles() if s <= c["t_ms"] <= e]
                        if iv == "1m" else [])
    out = fm.assemble("HYPE", now_ms=TAPE_NOW - 12_000, use_tape=True)
    assert out["taker_delta_source"] == {"used": "ws_recorder", "fallback_reason": None}
    assert out["taker_delta"]["5m"]["reliable"] is True
    assert out["flow"]["coverage_ok"] is True


def test_tape_that_starts_late_without_a_gap_marker_is_not_complete():
    # Only the last 150 s recorded, no gap record, the exchange traded all window.
    start = TAPE_NOW - 150_000
    rows = [{"t_ms": t, "buy_usdc": 100.0, "sell_usdc": 50.0, "count": 10}
            for t in range(start // MIN * MIN, TAPE_M + 1, MIN)]
    five = _five(_tape(rows=rows, first_trade_ms=start), _candles())
    assert five["capture_complete"] is False and five["reliable"] is False
    assert five["coverage_pct"] < 100.0
    tape = _tape(rows=rows, first_trade_ms=start)
    assert fm.tape_fallback_reason(fm.bucket_taker_delta_tape(tape, _candles()), tape) in (
        "capture incomplete", "coverage low")


def test_tape_with_a_silent_hole_in_a_traded_minute_is_not_complete():
    tape = _tape()
    tape["rows"] = [r for r in tape["rows"] if r["t_ms"] != TAPE_M - 2 * MIN]
    five = _five(tape, _candles())
    assert five["coverage_pct"] == 75.0                       # above the floor, still not complete
    assert five["capture_complete"] is False and five["reliable"] is False
    assert fm.tape_fallback_reason(fm.bucket_taker_delta_tape(tape, _candles()), tape) == "capture incomplete"


def test_a_tape_for_another_coin_is_refused():
    for body in (_tape(), _tape(coin=None)):
        got = fm.fetch_tape_taker_delta("ZEC", fetch_flow=lambda c, b=body: (b, None),
                                        candles=_candles_fn(), clock=lambda: TAPE_NOW)
        assert got == (None, "malformed tape")
    ok, why = fm.fetch_tape_taker_delta("hype", fetch_flow=lambda c: (_tape(), None),
                                        candles=_candles_fn(), clock=lambda: TAPE_NOW)
    assert why is None and ok["5m"]["reliable"] is True


def test_a_tape_that_ages_out_while_candles_are_fetched_is_stale():
    # last trade 45 s old at the first check; each of two candle calls takes 15 s
    clock = {"now": TAPE_NOW}

    def slow_candles(coin, iv, start, end):
        clock["now"] += 15_000
        return _candles_fn()(coin, iv, start, end)

    got = fm.fetch_tape_taker_delta("HYPE", fetch_flow=lambda c: (_tape(last_trade_ms=TAPE_NOW - 45_000), None),
                                    candles=slow_candles, clock=lambda: clock["now"])
    assert got == (None, "stale")


def test_the_last_closed_minute_is_inside_the_coverage_test():
    # Last trade fell in the minute that has just closed, where the tape caught
    # 1 of the exchange's 1,000 trades; nothing yet in the current minute.
    last_closed = TAPE_M - MIN
    tape = _tape(last_trade_ms=TAPE_M - 10_000)
    tape["rows"] = [dict(r, count=1) if r["t_ms"] == last_closed else r
                    for r in tape["rows"] if r["t_ms"] != TAPE_M]
    candles = [dict(c, n=1000) if c["t_ms"] == last_closed else c for c in _candles()]
    five = _five(tape, candles)
    assert five["sample_fresh"] is True
    assert five["coverage_pct"] == 3.0 and five["reliable"] is False
    assert fm.tape_fallback_reason(fm.bucket_taker_delta_tape(tape, candles), tape) == "coverage low"
    # ...and a traded last-closed minute with no tape row at all is a hole.
    tape["rows"] = [r for r in tape["rows"] if r["t_ms"] != last_closed]
    assert _five(tape, candles)["capture_complete"] is False
