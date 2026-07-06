import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_market as fm


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
