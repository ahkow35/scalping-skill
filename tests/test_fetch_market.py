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
