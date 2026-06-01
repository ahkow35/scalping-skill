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


import pytest


def test_fetch_btc_dominance_parses_coingecko():
    payload = {"data": {"market_cap_percentage": {"btc": 54.3},
                         "market_cap_change_percentage_24h_usd": 1.1}}
    out = fm.parse_btc_dominance(payload)
    assert out["btc_d"] == 54.3
    assert out["btc_d_24h_chg"] == 1.1


def test_data_unavailable_raises_named_error():
    with pytest.raises(fm.DataUnavailable) as e:
        fm.parse_btc_dominance({"unexpected": True})
    assert "coingecko" in str(e.value).lower()


def test_assemble_marks_macro_unavailable_when_btcd_fails(monkeypatch):
    monkeypatch.setattr(fm, "fetch_ctx", lambda c, dex=None: {"coin": c, "mark": 46.0})
    monkeypatch.setattr(fm, "fetch_candles", lambda *a: [])
    monkeypatch.setattr(fm, "fetch_l2", lambda c: {"asks": {}, "bids": {}})

    def boom():
        raise fm.DataUnavailable("DATA UNAVAILABLE: coingecko (down)")
    monkeypatch.setattr(fm, "fetch_btc_dominance", boom)

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
