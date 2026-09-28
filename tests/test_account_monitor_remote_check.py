"""remote-check: fail-closed preflight against the always-on Railway watcher's
GET /report. No network and no macOS Keychain access anywhere here — the HTTP
GET and the Keychain reader are always injected fakes.
"""
import json

import pytest
import requests

import account_monitor as monitor
from test_account_observation import WALLET


URL = "https://scalping-skill-production.up.railway.app"
NOW = 1_700_000_000_000
SECRET = "watcher-secret-token-xyz"

WATCHER_REPORT = {"status": "CLEAR", "entry_allowed": True, "read_only": True,
                   "orders_changed": False, "daily_breach_latched": False,
                   "reasons": [], "checked_at_ms": NOW - 5_000,
                   "open_trigger_distance_risk_usdc": 20.0, "remaining_daily_budget_usdc": 150.0,
                   "daily": {"net_pnl_usdc": 0.0, "limit_usdc": 150.0, "baseline_quality": "near_reset_observation",
                             "baseline_at_ms": NOW - 3_600_000, "net_cash_flow_usdc": 0.0},
                   "observation": {"equity_usdc": 5000.0, "unrealized_pnl_usdc": 0.0}}
WATCHER_HALT = {**WATCHER_REPORT, "status": "HALT", "entry_allowed": False}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, malformed=False, raw=None):
        self.status_code = status_code
        self._payload = payload
        self._malformed = malformed
        self._raw = raw

    @property
    def content(self):
        if self._raw is not None:
            return self._raw.encode()
        if self._malformed:
            return b"<html>not json</html>"
        return json.dumps(self._payload).encode()


def envelope(produced_at_ms, report=WATCHER_REPORT):
    return {"produced_at_ms": produced_at_ms, "report": report}


def clock(now=NOW):
    return lambda: now


def no_token():
    return None


def fixed_token(value=SECRET):
    return lambda: value


def refusing_keychain():
    pytest.fail("keychain must not be consulted when SCALP_WATCHER_TOKEN is set")


# --------------------------------------------------------------------------
# Success path
# --------------------------------------------------------------------------

def test_fresh_report_passes_through_entry_allowed_as_watcher_said():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        assert url == URL + "/report"
        assert headers["Authorization"] == f"Bearer {SECRET}"
        assert timeout == monitor.WATCHER_REQUEST_TIMEOUT_S
        return FakeResponse(200, envelope(NOW - 5_000))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CLEAR"
    assert report["entry_allowed"] is True
    assert report["source"] == "railway"
    assert report["report_age_s"] == pytest.approx(5.0)


def test_watcher_halt_passes_through_as_blocked():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW - 1_000, WATCHER_HALT))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "HALT"
    assert report["entry_allowed"] is False


# --------------------------------------------------------------------------
# Fail-closed: freshness
# --------------------------------------------------------------------------

def test_stale_report_blocks():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW - 75_000))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]
    assert report.get("observation") is None
    assert "75 s old" in report["reasons"][0]
    assert "limit 60 s" in report["reasons"][0]


def test_future_dated_report_blocks():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW + 10_000))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]
    assert "future" in report["reasons"][0]


def test_report_within_freshness_window_is_not_blocked():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW - 59_000))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CLEAR"
    assert report["entry_allowed"] is True


# --------------------------------------------------------------------------
# Fail-closed: transport / HTTP status
# --------------------------------------------------------------------------

def test_401_gives_refused_token_reason():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(401)
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]
    assert "refused the token" in report["reasons"][0]


def test_503_gives_unavailable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(503)
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]
    assert "503" in report["reasons"][0]


def test_connection_error_gives_unreachable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        raise requests.exceptions.ConnectionError("refused")
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]
    assert "unreachable" in report["reasons"][0]


def test_timeout_also_gives_unreachable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        raise requests.exceptions.Timeout("timed out")
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert "unreachable" in report["reasons"][0]


def test_malformed_json_gives_unavailable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, malformed=True)
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]


def test_missing_produced_at_ms_gives_unavailable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, {"report": WATCHER_REPORT})
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]


def test_missing_report_field_gives_unavailable():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, {"produced_at_ms": NOW})
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert not report["entry_allowed"]


# --------------------------------------------------------------------------
# Fail-closed: missing configuration
# --------------------------------------------------------------------------

def test_missing_url_gives_config_required(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("must not call the watcher with no URL configured")
    report = monitor.remote_check(tmp_path, env={}, get=forbidden, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CONFIG_REQUIRED"
    assert not report["entry_allowed"]
    assert report.get("observation") is None


def test_missing_token_env_unset_keychain_fails_gives_config_required(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("must not call the watcher with no token available")
    report = monitor.remote_check(
        tmp_path, env={"SCALP_WATCHER_URL": URL}, get=forbidden, keychain=no_token, clock=clock())
    assert report["status"] == "CONFIG_REQUIRED"
    assert not report["entry_allowed"]


def test_keychain_supplies_token_when_env_unset():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        assert headers["Authorization"] == f"Bearer {SECRET}"
        return FakeResponse(200, envelope(NOW - 1_000))
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL}, get=get, keychain=fixed_token(), clock=clock())
    assert report["entry_allowed"] is True


# --------------------------------------------------------------------------
# URL resolution: env overrides config, old configs keep working
# --------------------------------------------------------------------------

def test_env_url_overrides_config(tmp_path):
    monitor.atomic_json(tmp_path / "config.json", {"wallet": WALLET, "daily_loss_usdc": 100,
                                                     "watcher_url": "https://config-url.example.com"})
    seen = {}
    def get(url, headers=None, timeout=None, allow_redirects=None):
        seen["url"] = url
        return FakeResponse(200, envelope(NOW - 1_000))
    monitor.remote_check(tmp_path, env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
                          get=get, keychain=refusing_keychain, clock=clock())
    assert seen["url"] == URL + "/report"


def test_config_url_used_when_env_unset(tmp_path):
    monitor.atomic_json(tmp_path / "config.json", {"wallet": WALLET, "daily_loss_usdc": 100,
                                                     "watcher_url": URL})
    seen = {}
    def get(url, headers=None, timeout=None, allow_redirects=None):
        seen["url"] = url
        return FakeResponse(200, envelope(NOW - 1_000))
    report = monitor.remote_check(tmp_path, env={"SCALP_WATCHER_TOKEN": SECRET},
                                   get=get, keychain=refusing_keychain, clock=clock())
    assert seen["url"] == URL + "/report"
    assert report["entry_allowed"] is True


def test_old_config_without_watcher_url_keeps_working(tmp_path):
    # A config saved before this field existed — exactly the CONFIG fixture
    # used throughout the account_risk/account_monitor tests.
    monitor.atomic_json(tmp_path / "config.json", {"wallet": WALLET, "daily_loss_usdc": 100})
    def forbidden(*args, **kwargs):
        pytest.fail("must not call the watcher with no URL resolvable")
    report = monitor.remote_check(tmp_path, env={}, get=forbidden, keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CONFIG_REQUIRED"
    assert not report["entry_allowed"]


def test_corrupt_local_config_does_not_block_env_supplied_url(tmp_path):
    (tmp_path / "config.json").write_text("not json")
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW - 1_000))
    report = monitor.remote_check(tmp_path, env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
                                   get=get, keychain=refusing_keychain, clock=clock())
    assert report["entry_allowed"] is True


# --------------------------------------------------------------------------
# The token must never appear in any output, success or failure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("get,keychain_fn", [
    (lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(200, envelope(NOW - 1_000)), None),
    (lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(401), None),
    (lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(503), None),
    (lambda url, headers=None, timeout=None, allow_redirects=None: (_ for _ in ()).throw(requests.exceptions.ConnectionError("x")), None),
    (lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(200, malformed=True), None),
    (lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(200, envelope(NOW - 999_000)), None),
])
def test_token_never_appears_in_output(get, keychain_fn):
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    dumped = json.dumps(report)
    assert SECRET not in dumped
    assert SECRET not in monitor.render(report)


def test_token_never_appears_when_sourced_from_keychain():
    def get(url, headers=None, timeout=None, allow_redirects=None):
        return FakeResponse(200, envelope(NOW - 1_000))
    report = monitor.remote_check(env={"SCALP_WATCHER_URL": URL}, get=get,
                                   keychain=fixed_token(), clock=clock())
    assert SECRET not in json.dumps(report)
    assert SECRET not in monitor.render(report)


def test_keychain_reader_never_raises_and_never_leaks(monkeypatch, capsys):
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."

    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: FakeCompleted())
    assert monitor.read_keychain_watcher_token() is None
    assert capsys.readouterr().out == ""

    def raises(*a, **k):
        raise OSError("no such command")
    monkeypatch.setattr(monitor.subprocess, "run", raises)
    assert monitor.read_keychain_watcher_token() is None


def test_keychain_reader_returns_stripped_stdout(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = SECRET + "\n"
        stderr = ""
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: FakeCompleted())
    assert monitor.read_keychain_watcher_token() == SECRET


# --------------------------------------------------------------------------
# configure --watcher-url validation and old-config compatibility
# --------------------------------------------------------------------------

def test_configure_accepts_https_watcher_url(tmp_path):
    code = monitor.main(["--data-dir", str(tmp_path), "configure", "--wallet", WALLET,
                          "--daily-loss-usdc", "100", "--watcher-url", URL])
    assert code == 0
    saved = monitor.read_json(tmp_path / "config.json")
    assert saved["watcher_url"] == URL


def test_configure_rejects_non_https_watcher_url(tmp_path):
    code = monitor.main(["--data-dir", str(tmp_path), "configure", "--wallet", WALLET,
                          "--daily-loss-usdc", "100", "--watcher-url", "http://insecure.example.com"])
    assert code == 2
    assert not (tmp_path / "config.json").exists()


def test_configure_without_watcher_url_still_works(tmp_path):
    code = monitor.main(["--data-dir", str(tmp_path), "configure", "--wallet", WALLET,
                          "--daily-loss-usdc", "100"])
    assert code == 0
    saved = monitor.read_json(tmp_path / "config.json")
    assert saved.get("watcher_url") is None


def test_validate_watcher_url_rejects_garbage():
    with pytest.raises(monitor.AccountDataError):
        monitor.validate_watcher_url("not-a-url")
    with pytest.raises(monitor.AccountDataError):
        monitor.validate_watcher_url("ftp://example.com")
    assert monitor.validate_watcher_url(None) is None
    assert monitor.validate_watcher_url("https://example.com/") == "https://example.com"


# --------------------------------------------------------------------------
# CLI wiring: remote-check exit codes, never a silent local fallback
# --------------------------------------------------------------------------

def test_cli_remote_check_exit_zero_on_entry_allowed(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "remote_check", lambda data_dir: {**WATCHER_REPORT, "source": "railway",
                                                                     "report_age_s": 1.0})
    code = monitor.main(["remote-check", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "railway"


def test_cli_remote_check_exit_three_on_halt(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "remote_check", lambda data_dir: {**WATCHER_HALT, "source": "railway",
                                                                     "report_age_s": 1.0})
    code = monitor.main(["remote-check", "--json"])
    assert code == 3


def test_cli_remote_check_exit_two_on_config_required(monkeypatch, capsys):
    def fake_remote_check(data_dir):
        report = monitor.failure_result("watcher URL not configured")
        report["status"] = "CONFIG_REQUIRED"
        return report
    monkeypatch.setattr(monitor, "remote_check", fake_remote_check)
    code = monitor.main(["remote-check", "--json"])
    assert code == 2


def test_cli_never_leaks_token_on_connection_error(monkeypatch, capsys):
    """Drives the real main() -> remote_check() -> requests.get path (no
    explicit get= injection at the call site, the way the CLI actually runs)
    and checks the process's own stdout/stderr, not just the report dict."""
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.setenv("SCALP_WATCHER_TOKEN", SECRET)
    def raising_get(*a, **k):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(monitor.requests, "get", raising_get)
    code = monitor.main(["remote-check", "--json"])
    assert code == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_cli_never_leaks_token_on_401(monkeypatch, capsys):
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.setenv("SCALP_WATCHER_TOKEN", SECRET)
    monkeypatch.setattr(monitor.requests, "get", lambda *a, **k: FakeResponse(401))
    code = monitor.main(["remote-check", "--json"])
    assert code == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_cli_never_leaks_keychain_sourced_token(monkeypatch, capsys):
    """Token sourced from the Keychain (env unset), driven through the real
    CLI. subprocess.run is faked at the lowest level actually called by
    read_keychain_watcher_token, so no real `security` invocation happens."""
    class FakeCompleted:
        returncode = 0
        stdout = SECRET + "\n"
        stderr = ""
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.delenv("SCALP_WATCHER_TOKEN", raising=False)
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(monitor.requests, "get",
                         lambda *a, **k: FakeResponse(200, envelope(NOW - 1_000)))
    monitor.main(["remote-check", "--json"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_cli_remote_check_never_calls_local_check(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("remote-check must never fall back to a local check()")
    monkeypatch.setattr(monitor, "check", forbidden)
    monkeypatch.setattr(monitor, "remote_check", lambda data_dir: {**WATCHER_REPORT, "source": "railway",
                                                                     "report_age_s": 1.0})
    code = monitor.main(["remote-check", "--json"])
    assert code == 0


# --------------------------------------------------------------------------
# Garbled answers fail closed (an answer that is not plainly true/false, or a
# broken timestamp, must never read as permission)
# --------------------------------------------------------------------------

def _remote(payload, now=NOW):
    return monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(200, payload),
        keychain=refusing_keychain, clock=clock(now))


@pytest.mark.parametrize("entry_allowed", ["no", "yes", 1, 0, None, [], {}])
def test_non_boolean_entry_allowed_blocks(entry_allowed):
    report = _remote(envelope(NOW - 1_000, {**WATCHER_REPORT, "entry_allowed": entry_allowed}))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


@pytest.mark.parametrize("produced_at_ms", [float("nan"), float("inf"), float("-inf"), True, "1700000000000"])
def test_broken_timestamp_blocks(produced_at_ms):
    report = _remote(envelope(produced_at_ms))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


def test_non_finite_number_anywhere_in_report_blocks():
    report = _remote(envelope(NOW - 1_000, {**WATCHER_REPORT, "equity_usdc": float("nan")}))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


def test_nan_timestamp_cli_fails_closed_without_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.setenv("SCALP_WATCHER_TOKEN", SECRET)
    monkeypatch.setattr(monitor.requests, "get",
                        lambda url, headers=None, timeout=None, allow_redirects=None: FakeResponse(200, envelope(float("nan"))))
    code = monitor.main(["--data-dir", str(tmp_path), "remote-check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["entry_allowed"] is False


@pytest.mark.parametrize("age_ms,allowed", [(60_000, True), (60_001, False), (-5_000, True), (-5_001, False)])
def test_freshness_boundaries(age_ms, allowed):
    report = _remote(envelope(NOW - age_ms))
    assert report["entry_allowed"] is allowed


@pytest.mark.parametrize("watcher_url", [123, ["https://x"], {"u": 1}])
def test_non_string_config_url_is_config_required(tmp_path, watcher_url):
    (tmp_path / "config.json").write_text(json.dumps({"watcher_url": watcher_url}))
    report = monitor.remote_check(tmp_path, env={"SCALP_WATCHER_TOKEN": SECRET},
                                  get=lambda *a, **k: pytest.fail("must not fetch"),
                                  keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CONFIG_REQUIRED"
    assert report["entry_allowed"] is False


# --------------------------------------------------------------------------
# Cross-review round 1 (Codex): permission shape, transport, token echo, overflow
# --------------------------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"status": "HALT"},
    {"status": "SOMETHING_NEW"},
    {"daily_breach_latched": True},
    {"daily_breach_latched": None},
    {"daily": None},
    {"observation": None},
])
def test_permission_outside_a_complete_clear_blocks(override):
    report = _remote(envelope(NOW - 1_000, {**WATCHER_REPORT, **override}))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


def test_blocked_report_passes_through_without_full_shape():
    report = _remote(envelope(NOW - 1_000, {"status": "WARMUP", "entry_allowed": False, "reasons": []}))
    assert report["status"] == "WARMUP"
    assert report["entry_allowed"] is False


@pytest.mark.parametrize("url", ["http://scalping-skill-production.up.railway.app", "ftp://x", "not a url"])
def test_non_https_env_url_never_sends_the_token(url):
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": url, "SCALP_WATCHER_TOKEN": SECRET},
        get=lambda *a, **k: pytest.fail("token must not be sent"),
        keychain=refusing_keychain, clock=clock())
    assert report["status"] == "CONFIG_REQUIRED"
    assert report["entry_allowed"] is False


def test_redirects_are_not_followed():
    seen = {}

    def get(url, headers=None, timeout=None, allow_redirects=None):
        seen["allow_redirects"] = allow_redirects
        return FakeResponse(302, None)
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=get, keychain=refusing_keychain, clock=clock())
    assert seen["allow_redirects"] is False
    assert report["entry_allowed"] is False


def test_response_echoing_the_token_blocks_and_never_prints_it(capsys):
    echoed = {**WATCHER_REPORT, "reasons": [f"Authorization: Bearer {SECRET}"]}
    report = _remote(envelope(NOW - 1_000, echoed))
    assert report["entry_allowed"] is False
    assert SECRET not in json.dumps(report)
    assert SECRET not in monitor.render(report)


def test_overflowing_number_blocks():
    raw = json.dumps(envelope(NOW - 1_000)).replace('"equity_usdc": 5000.0', '"equity_usdc": 1e400')
    assert "1e400" in raw
    report = monitor.remote_check(
        env={"SCALP_WATCHER_URL": URL, "SCALP_WATCHER_TOKEN": SECRET},
        get=lambda *a, **k: FakeResponse(200, raw=raw), keychain=refusing_keychain, clock=clock())
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


# --------------------------------------------------------------------------
# Cross-review round 2 (Codex): permission needs the full CLEAR invariants;
# an escaped token echo is caught after decoding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"daily": {}, "observation": {}},
    {"observation": {}},
    {"observation": {"equity_usdc": 0.0}},
    {"daily": {**WATCHER_REPORT["daily"], "baseline_quality": "partial_day"}},
    {"daily": {**WATCHER_REPORT["daily"], "net_pnl_usdc": -150.0}},
    {"daily": {**WATCHER_REPORT["daily"], "limit_usdc": None}},
    {"open_trigger_distance_risk_usdc": None},
    {"remaining_daily_budget_usdc": None},
    {"open_trigger_distance_risk_usdc": 200.0},
    {"open_trigger_distance_risk_usdc": -1.0},
    {"remaining_daily_budget_usdc": True},
])
def test_permission_needs_every_clear_invariant(override):
    report = _remote(envelope(NOW - 1_000, {**WATCHER_REPORT, **override}))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


def test_complete_clear_fixture_is_permitted():
    report = _remote(envelope(NOW - 1_000))
    assert report["entry_allowed"] is True


@pytest.mark.parametrize("where", ["reason", "key"])
def test_escaped_token_echo_blocks_and_never_prints(where, capsys, monkeypatch):
    escaped = "".join(f"\\u{ord(c):04x}" for c in SECRET)
    base = json.dumps(envelope(NOW - 1_000))
    if where == "reason":
        raw = base.replace('"reasons": []', '"reasons": ["' + escaped + '"]')
    else:
        raw = base.replace('"reasons": []', '"reasons": [], "' + escaped + '": 1')
    assert SECRET not in raw
    body = FakeResponse(200, raw=raw)
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.setenv("SCALP_WATCHER_TOKEN", SECRET)
    monkeypatch.setattr(monitor.requests, "get", lambda *a, **k: body)
    code = monitor.main(["remote-check", "--json"])
    out = capsys.readouterr()
    assert code == 2
    assert json.loads(out.out)["entry_allowed"] is False
    assert SECRET not in out.out + out.err


# --------------------------------------------------------------------------
# Cross-review round 3 (Codex): remaining budget must match limit + P&L;
# baseline quality must be the recognised full-day value; huge ints block
# --------------------------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"daily": {**WATCHER_REPORT["daily"], "net_pnl_usdc": -90.0}},          # remaining 150 overstated (real 60)
    {"remaining_daily_budget_usdc": 149.0},
    {"daily": {**WATCHER_REPORT["daily"], "baseline_quality": "unknown"}},
    {"daily": {**WATCHER_REPORT["daily"], "baseline_quality": False}},
    {"daily": {**WATCHER_REPORT["daily"], "baseline_quality": {}}},
])
def test_round3_inconsistent_clear_blocks(override):
    report = _remote(envelope(NOW - 1_000, {**WATCHER_REPORT, **override}))
    assert report["status"] == "DATA_UNAVAILABLE"
    assert report["entry_allowed"] is False


def test_consistent_loss_day_still_permitted():
    report = _remote(envelope(NOW - 1_000, {
        **WATCHER_REPORT, "remaining_daily_budget_usdc": 60.0, "open_trigger_distance_risk_usdc": 60.0,
        "daily": {**WATCHER_REPORT["daily"], "net_pnl_usdc": -90.0}}))
    assert report["entry_allowed"] is True


def test_huge_integer_blocks_without_crashing(capsys, monkeypatch):
    raw = json.dumps(envelope(NOW - 1_000)).replace('"equity_usdc": 5000.0', '"equity_usdc": 1' + "0" * 400)
    monkeypatch.setenv("SCALP_WATCHER_URL", URL)
    monkeypatch.setenv("SCALP_WATCHER_TOKEN", SECRET)
    monkeypatch.setattr(monitor.requests, "get", lambda *a, **k: FakeResponse(200, raw=raw))
    code = monitor.main(["remote-check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["entry_allowed"] is False
