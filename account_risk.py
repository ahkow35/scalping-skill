"""Pure daily account-risk evaluation; a warning latch, not trade enforcement."""

import copy
from datetime import datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from account_api import AccountDataError, validate_wallet
from account_observation import accounting_events, normalize_snapshot, number


def validate_config(config):
    if not isinstance(config, dict):
        raise AccountDataError("account monitor configuration missing")
    result = dict(config)
    result["wallet"] = validate_wallet(config.get("wallet"))
    result["timezone"] = config.get("timezone", "Asia/Singapore")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise AccountDataError("invalid reset timezone") from exc
    kinds = [key for key in ("daily_loss_usdc", "daily_loss_pct") if config.get(key) is not None]
    if len(kinds) != 1:
        raise AccountDataError("configure exactly one daily loss limit: USDC or percent")
    value = number(config[kinds[0]], "daily loss limit")
    if value <= 0 or (kinds[0] == "daily_loss_pct" and value > 100):
        raise AccountDataError("daily loss limit must be positive (percentage at most 100)")
    result[kinds[0]] = value
    return result


def day_bounds(now_ms, timezone):
    zone = ZoneInfo(timezone)
    local_date = datetime.fromtimestamp(now_ms / 1000, zone).date()
    start = datetime.combine(local_date, day_time(), zone)
    end = datetime.combine(local_date + timedelta(days=1), day_time(), zone)
    return local_date.isoformat(), int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def history_start(config, state, now_ms):
    validate_state(state)
    key, start, _ = day_bounds(now_ms, config["timezone"])
    if state and state.get("day", {}).get("key") == key:
        return state["day"]["baseline_at_ms"]
    if state and state.get("last_observation"):
        return min(start, state["last_observation"]["asof_ms"])
    return start


def failure_result(reason, state=None):
    day = state.get("day", {}) if isinstance(state, dict) else {}
    day = day if isinstance(day, dict) else {}
    latched = day.get("tripped") is True
    return {"status": "HALT" if latched else "DATA_UNAVAILABLE", "entry_allowed": False,
            "read_only": True, "orders_changed": False, "daily_breach_latched": latched,
            "reasons": [reason], "daily": copy.deepcopy(day) if day else None}


def validate_state(state):
    if state is None:
        return
    if not isinstance(state, dict) or state.get("version") != 1:
        raise AccountDataError("unsupported or corrupt account risk state")
    day, previous = state.get("day"), state.get("last_observation")
    if not isinstance(day, dict) or not isinstance(previous, dict):
        raise AccountDataError("account risk state is incomplete; not resetting its baseline")
    for key in ("start_ms", "end_ms", "baseline_at_ms", "baseline_equity_usdc",
                "baseline_unrealized_usdc", "limit_usdc"):
        if not isinstance(day.get(key), (int, float)):
            raise AccountDataError("account risk state has an invalid daily field")
        number(day[key], key)
    for key in ("asof_ms", "equity_usdc", "unrealized_pnl_usdc"):
        if not isinstance(previous.get(key), (int, float)):
            raise AccountDataError("account risk state has an invalid observation")
        number(previous[key], key)
    if (not isinstance(day.get("tripped"), bool) or not isinstance(day.get("key"), str)
            or day.get("baseline_quality") not in {"partial_day", "near_reset_observation"}
            or day["limit_usdc"] < 0 or day["end_ms"] <= day["start_ms"]):
        raise AccountDataError("account risk state has invalid latch/baseline metadata")


def evaluate(snapshot, config, state=None, *, now_ms, max_age_ms=90_000, reset_grace_ms=60_000):
    """Return (report, next_state). Unknown inputs never yield entry_allowed.

    A first mid-day observation starts a partial-day baseline, not a fabricated
    midnight balance. That day stays WARMUP unless a known loss trips HALT.
    Around rollover, the previous observation is preferred so the interval
    straddling midnight is not silently discarded. Observation times are shown.
    """
    config = validate_config(config)
    validate_state(state)
    old = copy.deepcopy(state) if state is not None else None
    if old and (old.get("version") != 1 or old.get("wallet") != config["wallet"]
                or old.get("timezone") != config["timezone"]):
        raise AccountDataError("stored account identity/timezone changed; state has not been reset")
    observation = normalize_snapshot(snapshot, config["wallet"], now_ms, max_age_ms)
    report = {"read_only": True, "orders_changed": False, "entry_allowed": False,
              "daily_breach_latched": bool(old and old.get("day", {}).get("tripped")),
              "observation": observation, "reasons": list(observation["issues"])}
    if observation["issues"]:
        report.update(status="HALT" if report["daily_breach_latched"] else "UNSUPPORTED", daily=None)
        return report, old
    key, start, end = day_bounds(observation["asof_ms"], config["timezone"])
    candidate = copy.deepcopy(old) if old else {
        "version": 1, "wallet": config["wallet"], "timezone": config["timezone"],
    }
    day = candidate.get("day")
    if day and day["key"] > key:
        raise AccountDataError("exchange clock precedes stored risk day")
    if not day or day["key"] != key:
        base = observation
        near_reset = 0 <= observation["asof_ms"] - start <= reset_grace_ms
        previous = candidate.get("last_observation")
        if previous and 0 <= start - previous["asof_ms"] <= reset_grace_ms and near_reset:
            base = previous
        limit = config.get("daily_loss_usdc")
        if limit is None:
            limit = base["equity_usdc"] * config["daily_loss_pct"] / 100
        limit = max(0, limit)
        day = {"key": key, "start_ms": start, "end_ms": end,
               "baseline_at_ms": base["asof_ms"], "baseline_equity_usdc": base["equity_usdc"],
               "baseline_unrealized_usdc": base["unrealized_pnl_usdc"],
               "baseline_quality": "near_reset_observation" if near_reset else "partial_day",
               "limit_usdc": limit, "tripped": False, "tripped_at_ms": None}
        candidate["day"] = day
    if observation["asof_ms"] < day["baseline_at_ms"]:
        raise AccountDataError("observation precedes daily baseline")
    try:
        events = accounting_events(snapshot, day["baseline_at_ms"])
        equity_delta = observation["equity_usdc"] - day["baseline_equity_usdc"]
        net_pnl = equity_delta - events["net_cash_flow_usdc"]
        expected = (events["realized_net_usdc"] + observation["unrealized_pnl_usdc"]
                    - day["baseline_unrealized_usdc"])
        residual = net_pnl - expected
        # Financially material unexplained changes must not look like a clean
        # day. Small tolerance covers exchange rounding across many positions.
        if abs(residual) > 0.05:
            raise AccountDataError(f"accounting mismatch {residual:+.6f} USDC; transfers/fills or snapshot timing incomplete")
    except AccountDataError as exc:
        return {**report, **failure_result(str(exc), old)}, old
    if net_pnl <= -day["limit_usdc"] or observation["equity_usdc"] <= 0:
        if not day["tripped"]:
            day["tripped_at_ms"] = observation["asof_ms"]
        day["tripped"] = True
    uncovered = [stop["coin"] for stop in observation["stops"] if not stop["fully_covered"]]
    open_risk = None if uncovered else sum(stop["trigger_distance_risk_usdc"] for stop in observation["stops"])
    remaining = max(0, day["limit_usdc"] + net_pnl)
    report.update(
        status="CLEAR", daily_breach_latched=day["tripped"],
        open_trigger_distance_risk_usdc=open_risk, remaining_daily_budget_usdc=remaining,
        daily={**day, **events, "equity_change_usdc": equity_delta,
               "net_pnl_usdc": net_pnl, "reconciliation_residual_usdc": residual},
    )
    if day["tripped"]:
        report["status"] = "HALT"
        report["reasons"].append("daily account-loss limit breached; warning latched until next risk day")
    elif uncovered:
        report["status"] = "UNPROTECTED"
    elif open_risk > remaining:
        report["status"] = "EXCESS_OPEN_RISK"
        report["reasons"].append("open trigger-distance risk exceeds remaining daily budget, before execution costs")
    elif day["baseline_quality"] == "partial_day":
        report["status"] = "WARMUP"
    if uncovered:
        report["reasons"].append("stop coverage incomplete: " + ", ".join(uncovered))
    if day["baseline_quality"] == "partial_day":
        report["reasons"].append("baseline began mid-day; earlier equity loss is unknown")
    if observation["pending_entry_orders"] and report["status"] != "CLEAR":
        report["reasons"].append("pending non-reduce-only orders remain live; monitor cannot cancel them")
    report["entry_allowed"] = report["status"] == "CLEAR"
    report["reasons"].append("read-only observation; does not block manual orders or guarantee stop fills")
    candidate["last_observation"] = {key: observation[key] for key in (
        "asof_ms", "equity_usdc", "unrealized_pnl_usdc")}
    return report, candidate
