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
