"""Read-only account monitor CLI. Never signs, places, cancels or modifies orders."""

import argparse
import contextlib
import fcntl
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from account_api import AccountDataError, fetch_snapshot
from account_risk import evaluate, failure_result, history_start, validate_config


DEFAULT_DIR = Path(__file__).resolve().parent / ".account_monitor"

# remote-check: reads the always-on Railway watcher's /report instead of
# taking a local exchange reading. Fails closed; never falls back to check().
WATCHER_TOKEN_SERVICE = "scalp-watcher-report-token"
WATCHER_KEYCHAIN_TIMEOUT_S = 5
WATCHER_REQUEST_TIMEOUT_S = 10
FLOW_REQUEST_TIMEOUT_S = 5
WATCHER_FRESHNESS_S = 60
WATCHER_FUTURE_SKEW_S = 5


def read_json(path):
    def reject_constant(value):
        raise AccountDataError(f"non-finite JSON value: {value}")

    with path.open() as handle:
        value = json.load(handle, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise AccountDataError(f"{path.name} must contain a JSON object")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".", delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def observation_lock(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccountDataError("another account check is running; retry after it completes") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check(data_dir=DEFAULT_DIR, *, fetch=fetch_snapshot, clock=None):
    """Fetch, validate and atomically retain the daily latch. Errors never clear it."""
    data_dir = Path(data_dir)
    clock = clock or (lambda: int(time.time() * 1000))
    state = None
    try:
        with observation_lock(data_dir):
            config = validate_config(read_json(data_dir / "config.json"))
            state_path = data_dir / (config["wallet"] + ".json")
            if state_path.exists():
                state = read_json(state_path)
            now_ms = clock()
            snapshot = fetch(config["wallet"], history_start(config, state, now_ms))
            report, next_state = evaluate(snapshot, config, state, now_ms=clock())
            if next_state is not None:
                state = next_state
                atomic_json(state_path, next_state)
            report["checked_at_ms"] = clock()
            return report
    except FileNotFoundError as exc:
        report = failure_result(f"monitor configuration/state missing: {exc.filename}", state)
        report["status"] = "HALT" if report["daily_breach_latched"] else "CONFIG_REQUIRED"
        return report
    except (AccountDataError, OSError, ValueError, KeyError, TypeError) as exc:
        return failure_result(str(exc), state)


def validate_watcher_url(url):
    """https-only; None passes through (the field stays unconfigured, and an
    old config saved before this field existed keeps working)."""
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AccountDataError("watcher URL must be an https:// URL")
    return url.rstrip("/")


def read_keychain_watcher_token(service=WATCHER_TOKEN_SERVICE, timeout=WATCHER_KEYCHAIN_TIMEOUT_S):
    """Read the watcher report token from the macOS Keychain. Never raises and
    never returns anything but the token or None: a missing item, a non-mac
    host, or any subprocess failure is a silent None so the caller fails
    closed with CONFIG_REQUIRED. The token is never printed or logged here."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def _local_config(data_dir):
    """Best-effort read of config.json for its optional watcher_url. A
    missing or corrupt local config must not block remote-check, which does
    not otherwise depend on it (SCALP_WATCHER_URL alone is enough)."""
    try:
        config = read_json(Path(data_dir) / "config.json")
    except (FileNotFoundError, AccountDataError, OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a JSON integer too large for a float
        return False


def _complete_clear(report):
    """True only for a report carrying every invariant account_risk.evaluate
    guarantees when it grants entry: CLEAR, not latched, a full-day baseline,
    positive equity, P&L inside the limit, and known open risk that fits the
    remaining budget. Anything less must not read as permission."""
    daily = report.get("daily")
    observation = report.get("observation")
    if not (isinstance(daily, dict) and isinstance(observation, dict)):
        return False
    risk = report.get("open_trigger_distance_risk_usdc")
    remaining = report.get("remaining_daily_budget_usdc")
    net_pnl, limit = daily.get("net_pnl_usdc"), daily.get("limit_usdc")
    equity = observation.get("equity_usdc")
    return (report.get("status") == "CLEAR"
            and report.get("daily_breach_latched") is False
            and daily.get("baseline_quality") == "near_reset_observation"
            and all(_number(v) for v in (risk, remaining, net_pnl, limit, equity))
            and equity > 0 and limit > 0 and net_pnl > -limit
            # evaluate computes remaining as max(0, limit + net_pnl); an
            # overstated remaining would let open risk look like it fits.
            and abs(remaining - max(0, limit + net_pnl)) <= 0.01
            and 0 <= risk <= remaining)


def watcher_report_problem(report, age_s):
    """Why a watcher report must not be trusted as it stands, or None. The
    one gate both remote_check (/scalp) and the watcher's Telegram /check
    apply, so the two can never disagree about whether entry is allowed."""
    if report.get("entry_allowed") and not _complete_clear(report):
        return "watcher report was inconsistent (entry allowed outside a complete CLEAR)"
    if age_s > WATCHER_FRESHNESS_S:
        return f"watcher report is {age_s:.0f} s old (limit {WATCHER_FRESHNESS_S} s)"
    if age_s < -WATCHER_FUTURE_SKEW_S:
        return f"watcher report is {-age_s:.0f} s in the future (limit {WATCHER_FUTURE_SKEW_S} s)"
    return None


def resolve_watcher_access(data_dir, env, keychain):
    """(url, token, None) for the Railway watcher, or (None, None, (reason,
    status)) when the URL or token is missing/invalid. The one place both
    remote_check (/report) and remote_flow (/flow) read the watcher URL
    (SCALP_WATCHER_URL or the saved config) and the token (SCALP_WATCHER_TOKEN
    or Keychain). Never prints or logs the token."""
    url = env.get("SCALP_WATCHER_URL") or _local_config(data_dir).get("watcher_url")
    if not isinstance(url, str) or not url:
        return None, None, ("watcher URL not configured (configure --watcher-url or SCALP_WATCHER_URL)",
                            "CONFIG_REQUIRED")
    try:
        url = validate_watcher_url(url)
    except AccountDataError:
        return None, None, ("watcher URL must be an https:// URL", "CONFIG_REQUIRED")
    token = env.get("SCALP_WATCHER_TOKEN") or keychain()
    if not token:
        return None, None, ("watcher token not configured (SCALP_WATCHER_TOKEN or Keychain)",
                            "CONFIG_REQUIRED")
    return url, token, None


def remote_flow(coin, data_dir=DEFAULT_DIR, *, env=None, get=None, keychain=None,
                timeout=FLOW_REQUEST_TIMEOUT_S):
    """(payload, None) from the watcher's token-protected GET /flow?coin=X, or
    (None, reason) with reason one of "watcher not configured", "unauthorized",
    "bad request", "recorder not configured", "coin not recorded",
    "unreachable" or "malformed tape". Read-only; never raises and never
    includes the token, the Authorization header or any response text."""
    env = os.environ if env is None else env
    get = requests.get if get is None else get
    keychain = read_keychain_watcher_token if keychain is None else keychain
    url, token, problem = resolve_watcher_access(data_dir, env, keychain)
    if problem:
        return None, "watcher not configured"
    try:
        response = get(url + "/flow", params={"coin": coin}, headers={"Authorization": f"Bearer {token}"},
                       timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return None, "unreachable"
    status = response.status_code
    if status == 401:
        return None, "unauthorized"
    if status == 400:
        return None, "bad request"
    if status == 503:
        return None, "recorder not configured"
    if status not in (200, 404):
        return None, "unreachable"
    try:
        if token.encode() in response.content:
            return None, "malformed tape"
        payload = json.loads(response.content)
    except (ValueError, TypeError):
        # A 404 that is not the recorder's JSON is a watcher without /flow.
        return None, ("unreachable" if status == 404 else "malformed tape")
    if not isinstance(payload, dict):
        return None, "unreachable" if status == 404 else "malformed tape"
    if status == 404:
        return None, ("coin not recorded" if payload.get("error") == "coin_not_recorded"
                      else "unreachable")
    return payload, None


def remote_check(data_dir=DEFAULT_DIR, *, env=None, get=None,
                  keychain=None, clock=None,
                  timeout=WATCHER_REQUEST_TIMEOUT_S):
    """One read-only preflight read from the always-on Railway watcher's
    /report, in the same report shape as check(), plus source and
    report_age_s. Fails closed on any missing config, transport error,
    malformed body or stale/future report — entry_allowed false, no
    observation — and never falls back to a local check(). Never prints,
    logs or includes the token or the Authorization header.

    get and keychain default to None (resolved to requests.get and
    read_keychain_watcher_token here, not bound as default-argument values)
    so tests can intercept the real call path via monkeypatch even when
    invoked indirectly through main(), with no default-argument binding
    gotcha letting a real network or Keychain call slip through."""
    env = os.environ if env is None else env
    get = requests.get if get is None else get
    keychain = read_keychain_watcher_token if keychain is None else keychain
    clock = clock or (lambda: int(time.time() * 1000))

    def unavailable(reason, status="DATA_UNAVAILABLE"):
        report = failure_result(reason)
        report["status"] = status
        return report

    url, token, problem = resolve_watcher_access(data_dir, env, keychain)
    if problem:
        return unavailable(*problem)
    try:
        response = get(url + "/report", headers={"Authorization": f"Bearer {token}"},
                       timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return unavailable("watcher unreachable")
    if response.status_code == 401:
        return unavailable("watcher refused the token")
    if response.status_code != 200:
        return unavailable(f"watcher returned HTTP {response.status_code}")

    def reject_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):  # 1e400 overflows to inf without parse_constant
            raise ValueError(f"non-finite JSON number: {text}")
        return value

    try:
        if token.encode() in response.content:
            return unavailable("watcher response contained the token")
        payload = json.loads(response.content, parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, TypeError):
        return unavailable("watcher response was not valid JSON")
    # Decoded text too: a \uXXXX-escaped echo is absent from the raw bytes.
    if token in json.dumps(payload, ensure_ascii=False):
        return unavailable("watcher response contained the token")
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), dict):
        return unavailable("watcher response was malformed")
    produced_at_ms = payload.get("produced_at_ms")
    if isinstance(produced_at_ms, bool) or not isinstance(produced_at_ms, (int, float)):
        return unavailable("watcher response missing produced_at_ms")
    watcher_report = payload["report"]
    if (not isinstance(watcher_report.get("status"), str)
            or not isinstance(watcher_report.get("entry_allowed"), bool)):
        return unavailable("watcher response was malformed")
    age_s = (clock() - produced_at_ms) / 1000
    problem = watcher_report_problem(watcher_report, age_s)
    if problem:
        return unavailable(problem)
    result = dict(watcher_report)
    result["source"] = "railway"
    result["report_age_s"] = age_s
    return result


def render(report):
    lines = [f"ACCOUNT MONITOR — {report['status']} | read-only | new skill entries: "
             + ("eligible for further checks" if report["entry_allowed"] else "blocked")]
    observation = report.get("observation") or {}
    daily = report.get("daily") or {}
    equity = observation.get("equity_usdc")
    if equity is not None:
        lines.append(f"Equity {equity:,.2f} USDC | unrealized {observation['unrealized_pnl_usdc']:+,.2f} USDC")
    if daily.get("net_pnl_usdc") is not None:
        lines.append(f"Observed net P&L {daily['net_pnl_usdc']:+,.2f} USDC | daily limit {daily['limit_usdc']:,.2f} USDC")
        lines.append(f"Baseline {daily['baseline_quality']} at {daily['baseline_at_ms']} ms UTC | "
                     f"net cash flow {daily['net_cash_flow_usdc']:+,.2f} USDC")
    for stop in observation.get("stops", []):
        detail = "COVERED" if stop["fully_covered"] else "INCOMPLETE"
        if stop["has_stop_limit"]:
            detail += " (stop-limit may remain unfilled)"
        lines.append(f"{stop['coin']} {stop['side']}: stops {stop['covered_size']:g}/{stop['position_size']:g} — {detail}")
    lines.extend("- " + reason for reason in report.get("reasons", []))
    if observation.get("scope"):
        lines.append("Scope: " + observation["scope"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="save public wallet and user-selected loss limit locally")
    configure.add_argument("--wallet", required=True)
    limit = configure.add_mutually_exclusive_group(required=True)
    limit.add_argument("--daily-loss-usdc", type=float)
    limit.add_argument("--daily-loss-pct", type=float)
    configure.add_argument("--timezone", default="Asia/Singapore")
    configure.add_argument("--max-position-notional-usdc", type=float, default=None,
                            dest="max_position_notional_usdc",
                            help="optional per-position notional warning cap (owner-selected; no default)")
    configure.add_argument("--watcher-url", default=None, dest="watcher_url",
                            help="optional https URL of the always-on Railway watcher "
                                 "(remote-check reads GET <url>/report)")
    single = commands.add_parser("check", help="one fresh read-only observation")
    single.add_argument("--json", action="store_true")
    remote = commands.add_parser("remote-check", help="one read-only observation from the "
                                  "always-on Railway watcher (fails closed; never falls back to a local check)")
    remote.add_argument("--json", action="store_true")
    watch = commands.add_parser("watch", help="keep observing; prints locally only")
    watch.add_argument("--json", action="store_true")
    watch.add_argument("--interval-seconds", type=int, default=30)
    watch.add_argument("--count", type=int, default=0, help="0 means until interrupted")
    args = parser.parse_args(argv)
    if args.command == "configure":
        try:
            watcher_url = validate_watcher_url(args.watcher_url)
            config = validate_config({"wallet": args.wallet, "timezone": args.timezone,
                                      "daily_loss_usdc": args.daily_loss_usdc,
                                      "daily_loss_pct": args.daily_loss_pct,
                                      "max_position_notional_usdc": args.max_position_notional_usdc,
                                      "watcher_url": watcher_url})
            with observation_lock(args.data_dir):
                existing = args.data_dir / (config["wallet"] + ".json")
                if existing.exists() and read_json(existing).get("timezone") != config["timezone"]:
                    raise AccountDataError("cannot change reset timezone while account risk state exists")
                atomic_json(args.data_dir / "config.json", config)
            print(json.dumps({"configured": True, "wallet": config["wallet"],
                              "read_only": True, "note": "Existing daily latch retained; changed loss limit takes effect next risk day."}))
            return 0
        except (AccountDataError, OSError, ValueError) as exc:
            print(json.dumps(failure_result(str(exc))))
            return 2
    if args.command == "watch" and (args.interval_seconds < 10 or args.count < 0):
        parser.error("watch interval must be at least 10 seconds and count nonnegative")
    count = 0
    try:
        while True:
            report = remote_check(args.data_dir) if args.command == "remote-check" else check(args.data_dir)
            print(json.dumps(report, allow_nan=False) if args.json else render(report), flush=True)
            count += 1
            if args.command in ("check", "remote-check") or (args.count and count >= args.count):
                return 0 if report["entry_allowed"] else (3 if report["status"] == "HALT" else 2)
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
