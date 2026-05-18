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
