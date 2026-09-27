"""Read-only account monitor CLI. Never signs, places, cancels or modifies orders."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
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

    url = env.get("SCALP_WATCHER_URL") or _local_config(data_dir).get("watcher_url")
    if not url:
        return unavailable("watcher URL not configured (configure --watcher-url or SCALP_WATCHER_URL)",
                            status="CONFIG_REQUIRED")
    url = url.rstrip("/")
    token = env.get("SCALP_WATCHER_TOKEN") or keychain()
    if not token:
        return unavailable("watcher token not configured (SCALP_WATCHER_TOKEN or Keychain)",
                            status="CONFIG_REQUIRED")
    try:
        response = get(url + "/report", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except requests.RequestException:
        return unavailable("watcher unreachable")
    if response.status_code == 401:
        return unavailable("watcher refused the token")
    if response.status_code != 200:
        return unavailable(f"watcher returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return unavailable("watcher response was not valid JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), dict):
        return unavailable("watcher response was malformed")
    produced_at_ms = payload.get("produced_at_ms")
    if not isinstance(produced_at_ms, (int, float)):
        return unavailable("watcher response missing produced_at_ms")
    watcher_report = payload["report"]
    if not isinstance(watcher_report.get("status"), str) or "entry_allowed" not in watcher_report:
        return unavailable("watcher response was malformed")
    age_s = (clock() - produced_at_ms) / 1000
    if age_s > WATCHER_FRESHNESS_S:
        return unavailable(f"watcher report is {age_s:.0f} s old (limit {WATCHER_FRESHNESS_S} s)")
    if age_s < -WATCHER_FUTURE_SKEW_S:
        return unavailable(f"watcher report is {-age_s:.0f} s in the future (limit {WATCHER_FUTURE_SKEW_S} s)")
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
