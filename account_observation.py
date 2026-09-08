"""Validate observed balances, protective orders and accounting events."""

import json
import math

from account_api import AccountDataError, validate_wallet


def number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountDataError(f"invalid {label}") from exc
    if isinstance(value, bool) or not math.isfinite(result):
        raise AccountDataError(f"non-finite or invalid {label}")
    return result


def stop_coverage(position, orders):
    """Count only active top-level, reduce-only stops with remaining quantity.

    Parent-order children and zero-size/dynamic conventions are not assumed to
    be protective. Stop-limit presence is reported separately from execution.
    """
    qty, mark = abs(position["size"]), position["mark"]
    long = position["size"] > 0
    candidates, rejected = [], []
    seen: dict[object, str] = {}
    for order in orders:
        if not isinstance(order, dict):
            raise AccountDataError("malformed open order")
        if order.get("coin") != position["coin"]:
            continue
        kind = str(order.get("orderType", "")).lower()
        if kind not in {"stop market", "stop limit"}:
            continue
        oid = order.get("oid")
        encoded = json.dumps(order, sort_keys=True)
        if not isinstance(oid, (int, str)) or isinstance(oid, bool):
            rejected.append("stop has no order ID")
            continue
        if oid in seen:
            if seen[oid] != encoded:
                raise AccountDataError("conflicting duplicate protective order")
            continue
        seen[oid] = encoded
        if (order.get("reduceOnly") is not True or order.get("isTrigger") is not True
                or order.get("side") != ("A" if long else "B")):
            rejected.append(f"order {oid}: not an active reduce-only stop for this side")
            continue
        trigger = number(order.get("triggerPx"), "stop trigger")
        remaining = number(order.get("sz"), "remaining stop size")
        if trigger <= 0 or remaining <= 0:
            rejected.append(f"order {oid}: zero/invalid size or dynamic coverage unverified")
            continue
        if (long and trigger >= mark) or (not long and trigger <= mark):
            rejected.append(f"order {oid}: trigger already crossed or on wrong side of mark")
            continue
        liq = position.get("liquidation_price")
        if liq and ((long and trigger <= liq) or (not long and trigger >= liq)):
            rejected.append(f"order {oid}: trigger beyond liquidation price")
            continue
        candidates.append({"oid": oid, "size": remaining, "trigger": trigger,
                           "order_type": kind, "distance": abs(mark - trigger)})
    covered = min(qty, sum(stop["size"] for stop in candidates))
    complete = covered >= qty - max(1e-10, qty * 1e-9)
    # If several stops overlap, attribute quantity to the farthest first. This
    # is conservative trigger-distance exposure, not a guaranteed stopped loss.
    left, distance_loss = qty, 0.0
    for stop in sorted(candidates, key=lambda stop: stop["distance"], reverse=True):
        amount = min(left, stop["size"])
        distance_loss += amount * stop["distance"]
        left -= amount
    return {
        "coin": position["coin"], "side": "long" if long else "short",
        "position_size": qty, "covered_size": covered, "fully_covered": complete,
        "stop_orders": candidates, "rejected": rejected,
        "has_stop_limit": any(stop["order_type"] == "stop limit" for stop in candidates),
        "trigger_distance_risk_usdc": round(distance_loss, 6) if complete else None,
        "execution_guaranteed": False,
    }


def normalize_snapshot(snapshot, wallet, now_ms, max_age_ms):
    """Return validated observations and explicit equity-support limitations."""
    if not isinstance(snapshot, dict) or snapshot.get("wallet") != validate_wallet(wallet):
        raise AccountDataError("snapshot belongs to another wallet or is malformed")
    issues = []
    # 'default' is an exchange-selected mode, not evidence of separate balances.
    if snapshot.get("mode") != "disabled":
        issues.append(f"unsupported account mode {snapshot.get('mode')!r}; standard mode required for equity aggregation")
    venues = snapshot.get("venues")
    if not isinstance(venues, dict) or "" not in venues:
        raise AccountDataError("core perpetual account observation missing")
    received = number(snapshot.get("received_at_ms"), "receipt time")
    started = number(snapshot.get("started_at_ms"), "request start time")
    if not 0 <= now_ms - received <= max_age_ms or not 0 <= received - started <= max_age_ms:
        raise AccountDataError("stale or incoherent account observation")
    equity, unrealized, positions, stops, times, pending_entries = 0.0, 0.0, [], [], [], 0
    for dex, value in venues.items():
        if not isinstance(value, dict):
            raise AccountDataError("malformed venue observation")
        state, orders, meta = value.get("state"), value.get("orders"), value.get("meta")
        if not isinstance(state, dict) or not isinstance(orders, list) or not isinstance(meta, dict):
            raise AccountDataError(f"{dex or 'core'} balance/orders/metadata incomplete")
        stamp = number(state.get("time"), "exchange account time")
        if not 0 <= now_ms - stamp <= max_age_ms:
            raise AccountDataError(f"{dex or 'core'} account snapshot is stale or future-dated")
        times.append(stamp)
        summary, raw_positions = state.get("marginSummary"), state.get("assetPositions")
        if not isinstance(summary, dict) or not isinstance(raw_positions, list):
            raise AccountDataError(f"{dex or 'core'} account schema incomplete")
        balance = number(summary.get("accountValue"), "account equity")
        equity += balance
        active = bool(balance or raw_positions or orders)
        if active and (type(meta.get("collateralToken")) is not int or meta["collateralToken"] != 0):
            issues.append(f"{dex or 'core'} collateral is not verified USDC")
        for order in orders:
            if not isinstance(order, dict):
                raise AccountDataError("malformed open order")
            if order.get("reduceOnly") is not True:
                pending_entries += 1
        for wrapper in raw_positions:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("position"), dict):
                raise AccountDataError("malformed position")
            raw = wrapper["position"]
            size = number(raw.get("szi"), "position size")
            if size == 0:
                continue
            coin = raw.get("coin")
            if not isinstance(coin, str) or not coin or (coin.split(":")[0] if ":" in coin else "") != dex:
                raise AccountDataError("position venue does not match its coin")
            value_usdc = number(raw.get("positionValue"), "position value")
            entry = number(raw.get("entryPx"), "entry price")
            upnl = number(raw.get("unrealizedPnl"), "unrealized P&L")
            liq = raw.get("liquidationPx")
            liq = number(liq, "liquidation price") if liq is not None else None
            if value_usdc <= 0 or entry <= 0 or (liq is not None and liq < 0):
                raise AccountDataError("non-positive position price/value")
            position = {"coin": coin, "size": size, "mark": value_usdc / abs(size),
                        "entry": entry, "unrealized_pnl_usdc": upnl, "liquidation_price": liq}
            positions.append(position)
            unrealized += upnl
            stops.append(stop_coverage(position, orders))
    asof = number(snapshot.get("asof_ms"), "snapshot timestamp")
    if asof != max(times):
        raise AccountDataError("aggregate snapshot timestamp disagrees with venue observations")
    return {
        "wallet": wallet, "asof_ms": int(asof), "mode": snapshot.get("mode"),
        "equity_usdc": equity if not issues else None,
        "unrealized_pnl_usdc": unrealized if not issues else None,
        "positions": positions, "stops": stops, "issues": issues,
        "venues": sorted(venues), "pending_entry_orders": pending_entries,
        "scope": "named wallet, standard-mode USDC perpetual balances; excludes spot, vaults and other wallets/subaccounts",
    }


def _events(snapshot, key, since_ms):
    rows = snapshot.get(key)
    if not isinstance(rows, list):
        raise AccountDataError(f"{key} history missing")
    unique: dict[object, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise AccountDataError(f"malformed {key} event")
        stamp = number(row.get("time"), "event timestamp")
        if not since_ms < stamp <= snapshot["asof_ms"]:
            continue
        identity = (stamp, row.get("coin"), row.get("tid")) if key == "fills" else json.dumps(row, sort_keys=True)
        if identity in unique and unique[identity] != row:
            raise AccountDataError("conflicting duplicate fill identity")
        unique[identity] = row
    return list(unique.values())


def accounting_events(snapshot, since_ms):
    """Net fills/funding and known external cash flows since the observed base.

    Unknown ledger shapes fail closed. They are never silently interpreted as
    zero cash flow. The equity identity separately detects omissions or races.
    """
    if number(snapshot.get("history_start_ms"), "history start") > since_ms:
        raise AccountDataError("history starts after the equity baseline")
    closed, fees, funding, transfers, count = 0.0, 0.0, 0.0, 0.0, 0
    for fill in _events(snapshot, "fills", since_ms):
        coin = fill.get("coin", "")
        if not isinstance(coin, str) or not coin:
            raise AccountDataError("fill coin missing or malformed")
        if str(coin).startswith("@") or "/" in str(coin) or fill.get("dir") in {"Buy", "Sell"}:
            continue  # spot is outside the supported independent perp balances
        if (coin.split(":")[0] if ":" in coin else "") not in snapshot["venues"]:
            raise AccountDataError("fill belongs to an unobserved venue")
        if fill.get("tid") is None or str(fill.get("feeToken", "")).strip() != "USDC":
            raise AccountDataError("fill identity or USDC fee denomination unverified")
        closed += number(fill.get("closedPnl"), "closed P&L")
        fees += number(fill.get("fee"), "fill fee")  # already includes builder fees
        count += 1
    for row in _events(snapshot, "funding", since_ms):
        delta = row.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "funding":
            raise AccountDataError("malformed funding delta")
        coin = delta.get("coin")
        if (not isinstance(coin, str) or not coin
                or (coin.split(":")[0] if ":" in coin else "") not in snapshot["venues"]):
            raise AccountDataError("funding venue unverified")
        funding += number(delta.get("usdc"), "funding payment")
    for row in _events(snapshot, "ledger", since_ms):
        delta = row.get("delta")
        if not isinstance(delta, dict):
            raise AccountDataError("malformed non-funding ledger delta")
        kind = delta.get("type")
        if "usdc" in delta and number(delta["usdc"], "transfer amount") < 0:
            raise AccountDataError("negative transfer principal")
        if kind == "deposit":
            transfers += number(delta.get("usdc"), "deposit")
        elif kind == "withdraw":
            transfers -= number(delta.get("usdc"), "withdrawal")
        elif kind == "accountClassTransfer" and isinstance(delta.get("toPerp"), bool):
            transfers += (1 if delta["toPerp"] else -1) * number(delta.get("usdc"), "class transfer")
        elif kind in {"internalTransfer", "subAccountTransfer"}:
            wallet = snapshot["wallet"]
            source, destination = str(delta.get("user", "")).lower(), str(delta.get("destination", "")).lower()
            if wallet not in {source, destination}:
                raise AccountDataError("transfer counterparties unavailable")
            transfers += ((destination == wallet) - (source == wallet)) * number(delta.get("usdc"), "transfer")
        else:
            raise AccountDataError(f"unreconciled ledger type {kind!r}; equity loss unknown")
    return {"closed_pnl_usdc": closed, "fees_usdc": fees, "funding_usdc": funding,
            "net_cash_flow_usdc": transfers, "realized_net_usdc": closed - fees + funding,
            "fill_count": count}
