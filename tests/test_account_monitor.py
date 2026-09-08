import pytest

import account_monitor as monitor
from test_account_observation import WALLET, snapshot
from test_account_risk import CONFIG, MIDNIGHT


def test_missing_configuration_never_fetches_or_allows_entry(tmp_path):
    def forbidden(*args):
        pytest.fail("unconfigured monitor must not fetch")
    report = monitor.check(tmp_path, fetch=forbidden)
    assert report["status"] == "CONFIG_REQUIRED"
    assert not report["entry_allowed"]


def test_restart_retains_halt_on_network_error(tmp_path):
    monitor.atomic_json(tmp_path / "config.json", CONFIG)
    monitor.check(tmp_path, fetch=lambda *args: snapshot(now=MIDNIGHT), clock=lambda: MIDNIGHT)
    report = monitor.check(tmp_path, fetch=lambda *args: snapshot(now=MIDNIGHT + 1000,
                           equity=880, upnl=-120, size=10), clock=lambda: MIDNIGHT + 1000)
    assert report["status"] == "HALT"
    def unavailable(*args):
        raise OSError("offline")
    report = monitor.check(tmp_path, fetch=unavailable, clock=lambda: MIDNIGHT + 2000)
    assert report["status"] == "HALT"
    assert monitor.read_json(tmp_path / (WALLET + ".json"))["day"]["tripped"]


def test_failed_state_write_does_not_report_new_breach_as_clear(tmp_path, monkeypatch):
    monitor.atomic_json(tmp_path / "config.json", CONFIG)
    monitor.check(tmp_path, fetch=lambda *args: snapshot(now=MIDNIGHT), clock=lambda: MIDNIGHT)
    def failed_write(*args):
        raise OSError("disk full")
    monkeypatch.setattr(monitor, "atomic_json", failed_write)
    report = monitor.check(tmp_path, fetch=lambda *args: snapshot(now=MIDNIGHT + 1000,
                           equity=880, upnl=-120, size=10), clock=lambda: MIDNIGHT + 1000)
    assert report["status"] == "HALT"
    assert "disk full" in report["reasons"][0]


def test_overlapping_checks_fail_closed(tmp_path):
    with monitor.observation_lock(tmp_path):
        assert not monitor.check(tmp_path)["entry_allowed"]
