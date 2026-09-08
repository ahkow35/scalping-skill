"""Read-only account monitor CLI. Never signs, places, cancels or modifies orders."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from account_api import AccountDataError, fetch_snapshot
from account_risk import evaluate, failure_result, history_start, validate_config


DEFAULT_DIR = Path(__file__).resolve().parent / ".account_monitor"


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
    single = commands.add_parser("check", help="one fresh read-only observation")
    single.add_argument("--json", action="store_true")
    watch = commands.add_parser("watch", help="keep observing; prints locally only")
    watch.add_argument("--json", action="store_true")
    watch.add_argument("--interval-seconds", type=int, default=30)
    watch.add_argument("--count", type=int, default=0, help="0 means until interrupted")
    args = parser.parse_args(argv)
    if args.command == "configure":
        try:
            config = validate_config({"wallet": args.wallet, "timezone": args.timezone,
                                      "daily_loss_usdc": args.daily_loss_usdc,
                                      "daily_loss_pct": args.daily_loss_pct})
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
            report = check(args.data_dir)
            print(json.dumps(report, allow_nan=False) if args.json else render(report), flush=True)
            count += 1
            if args.command == "check" or (args.count and count >= args.count):
                return 0 if report["entry_allowed"] else (3 if report["status"] == "HALT" else 2)
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
