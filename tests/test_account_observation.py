import copy

import pytest

from account_api import AccountDataError
from account_observation import accounting_events, normalize_snapshot, stop_coverage


WALLET = "0x" + "1" * 40
NOW = 1_800_000_000_000


def position(size=10, mark=100, liq=50):
    return {"coin": "HYPE", "size": size, "mark": mark, "liquidation_price": liq}


def stop(oid=1, size=10, trigger=90, side="A", **changes):
    return {"coin": "HYPE", "oid": oid, "sz": str(size), "origSz": "100",
            "triggerPx": str(trigger), "side": side, "orderType": "Stop Market",
            "reduceOnly": True, "isTrigger": True, **changes}


def snapshot(now=NOW, equity=1000, upnl=0, size=0, orders=None, mode="disabled"):
    positions = [] if size == 0 else [{"position": {
        "coin": "HYPE", "szi": str(size), "entryPx": "100",
        "positionValue": str(abs(size) * (100 + upnl / size)),
        "unrealizedPnl": str(upnl), "liquidationPx": "50" if size > 0 else "150",
    }}]
    return {"version": 1, "wallet": WALLET, "mode": mode,
            "started_at_ms": now, "received_at_ms": now, "asof_ms": now,
            "history_start_ms": now - 86_400_000,
            "venues": {"": {"state": {"time": now, "marginSummary": {"accountValue": str(equity)},
                                      "assetPositions": positions},
                              "orders": orders or [], "meta": {"collateralToken": 0}}},
            "fills": [], "funding": [], "ledger": []}


def test_remaining_size_not_original_size_controls_coverage():
    result = stop_coverage(position(), [stop(size=2)])
    assert result["covered_size"] == 2
    assert not result["fully_covered"]


def test_duplicate_orders_cannot_manufacture_full_coverage():
    order = stop(size=5)
    assert stop_coverage(position(), [order, order])["covered_size"] == 5


@pytest.mark.parametrize("order", [
    stop(side="B"), stop(reduceOnly=False), stop(isTrigger=False),
    stop(size=0, isPositionTpsl=True), stop(trigger=101), stop(trigger=49),
    stop(orderType="Take Profit Market"),
])
def test_nonprotective_orders_do_not_count(order):
    assert not stop_coverage(position(), [order])["fully_covered"]


def test_short_stop_and_stop_limit_are_audited_without_execution_promise():
    result = stop_coverage(position(size=-10, liq=150), [stop(trigger=110, side="B", orderType="Stop Limit")])
    assert result["fully_covered"] and result["has_stop_limit"]
    assert result["execution_guaranteed"] is False


def test_parent_children_do_not_count_as_active_protection():
    parent = {"coin": "HYPE", "orderType": "Limit", "children": [stop()]}
    assert not stop_coverage(position(), [parent])["fully_covered"]


def test_unhashable_or_boolean_order_ids_cannot_crash_or_count():
    assert not stop_coverage(position(), [stop(oid=[]), stop(oid=True)])["fully_covered"]


@pytest.mark.parametrize("mode", ["portfolioMargin", "dexAbstraction", "default", None])
def test_shared_or_unspecified_balances_are_not_summed_as_equity(mode):
    result = normalize_snapshot(snapshot(mode=mode), WALLET, NOW, 90_000)
    assert result["equity_usdc"] is None and result["issues"]


def test_other_collateral_with_exposure_makes_equity_unknown():
    data = snapshot()
    data["venues"]["xyz"] = copy.deepcopy(data["venues"][""])
    data["venues"]["xyz"]["meta"]["collateralToken"] = 1105
    result = normalize_snapshot(data, WALLET, NOW, 90_000)
    assert result["equity_usdc"] is None


def test_stale_exchange_snapshot_fails_even_when_just_received():
    data = snapshot()
    data["venues"][""]["state"]["time"] = NOW - 91_000
    with pytest.raises(AccountDataError, match="stale"):
        normalize_snapshot(data, WALLET, NOW, 90_000)


@pytest.mark.parametrize("value", ["nan", "inf", None, True])
def test_invalid_account_values_never_look_like_zero(value):
    data = snapshot()
    data["venues"][""]["state"]["marginSummary"]["accountValue"] = value
    with pytest.raises(AccountDataError):
        normalize_snapshot(data, WALLET, NOW, 90_000)


def test_fill_fees_already_include_builder_fee_and_funding_is_signed():
    data = snapshot()
    fill = {"coin": "HYPE", "tid": 1, "time": NOW, "feeToken": "USDC",
            "closedPnl": "20", "fee": "2", "builderFee": "1"}
    data["fills"] = [fill, fill]
    data["funding"] = [{"time": NOW, "delta": {"type": "funding", "coin": "HYPE", "usdc": "-3"}}]
    result = accounting_events(data, NOW - 1000)
    assert result["realized_net_usdc"] == 15
    assert result["fill_count"] == 1


def test_deposit_withdrawal_and_spot_transfer_are_separate_from_trade_pnl():
    data = snapshot()
    data["ledger"] = [{"time": NOW, "delta": delta} for delta in [
        {"type": "deposit", "usdc": "100"}, {"type": "withdraw", "usdc": "30"},
        {"type": "accountClassTransfer", "usdc": "20", "toPerp": False},
    ]]
    result = accounting_events(data, NOW - 1000)
    assert result["net_cash_flow_usdc"] == 50
    assert result["realized_net_usdc"] == 0


def test_unknown_ledger_type_blocks_reconciliation():
    data = snapshot()
    data["ledger"] = [{"time": NOW, "delta": {"type": "mysteryTransfer", "usdc": "100"}}]
    with pytest.raises(AccountDataError, match="unreconciled"):
        accounting_events(data, NOW - 1000)


def spot(usdc="7000", hold="0", extra=()):
    balances = [{"coin": "USDC", "token": 0, "total": usdc, "hold": hold, "entryNtl": "0.0"},
                {"coin": "VAPOR", "token": 42, "total": "41041.3", "hold": "0.0", "entryNtl": "2887.6"}]
    return {"balances": balances + list(extra), "tokenToAvailableAfterMaintenance": [[0, usdc]]}


def unified(**changes):
    data = snapshot(mode="unifiedAccount", equity=0, **changes)
    data["spot"] = spot()
    return data


def test_unified_equity_is_spot_usdc_plus_unrealized_and_ignores_other_tokens():
    result = normalize_snapshot(unified(size=10, upnl=25), WALLET, NOW, 90_000)
    assert result["equity_usdc"] == 7025
    assert result["unrealized_pnl_usdc"] == 25
    assert result["issues"] == []
    assert len(result["positions"]) == 1 and len(result["stops"]) == 1
    assert "unified" in result["scope"]


def test_unified_ignores_perp_venue_account_value_to_avoid_double_counting():
    data = unified()
    data["venues"][""]["state"]["marginSummary"]["accountValue"] = "500"
    assert normalize_snapshot(data, WALLET, NOW, 90_000)["equity_usdc"] == 7000


def test_unified_missing_spot_balances_never_look_like_zero_equity():
    data = unified()
    del data["spot"]
    with pytest.raises(AccountDataError, match="spot"):
        normalize_snapshot(data, WALLET, NOW, 90_000)


@pytest.mark.parametrize("balances", [
    [],
    [{"coin": "USDC", "token": 0, "total": "1", "hold": "0"}, {"coin": "USDC", "token": 0, "total": "2", "hold": "0"}],
    [{"coin": "USDC", "token": 5, "total": "1", "hold": "0"}],
    [{"coin": "USDC", "token": 0, "total": "nan", "hold": "0"}],
])
def test_unified_ambiguous_or_invalid_usdc_balance_fails_closed(balances):
    data = unified()
    data["spot"]["balances"] = balances
    with pytest.raises(AccountDataError):
        normalize_snapshot(data, WALLET, NOW, 90_000)


def test_unified_spot_hold_means_spot_activity_and_unknown_equity():
    data = unified()
    data["spot"] = spot(hold="5")
    result = normalize_snapshot(data, WALLET, NOW, 90_000)
    assert result["equity_usdc"] is None and any("hold" in issue for issue in result["issues"])


def test_disabled_mode_still_sums_perp_balances_and_ignores_spot_block():
    data = snapshot(equity=1000)
    data["spot"] = spot()
    assert normalize_snapshot(data, WALLET, NOW, 90_000)["equity_usdc"] == 1000


@pytest.mark.parametrize("coin", ["@107", "VAPOR/USDC"])
def test_unified_spot_fill_blocks_reconciliation(coin):
    data = unified()
    data["fills"] = [{"coin": coin, "tid": 1, "time": NOW, "feeToken": "USDC",
                      "closedPnl": "0", "fee": "1", "dir": "Buy"}]
    with pytest.raises(AccountDataError, match="spot"):
        accounting_events(data, NOW - 1000)


def test_disabled_mode_still_skips_spot_fills():
    data = snapshot()
    data["fills"] = [{"coin": "@107", "tid": 1, "time": NOW, "feeToken": "USDC",
                      "closedPnl": "0", "fee": "1", "dir": "Buy"}]
    assert accounting_events(data, NOW - 1000)["fill_count"] == 0


def test_unified_class_transfer_is_unreconciled():
    data = unified()
    data["ledger"] = [{"time": NOW, "delta": {"type": "accountClassTransfer", "usdc": "20", "toPerp": True}}]
    with pytest.raises(AccountDataError, match="unreconciled"):
        accounting_events(data, NOW - 1000)


def test_unified_deposit_still_counts_as_cash_flow():
    data = unified()
    data["ledger"] = [{"time": NOW, "delta": {"type": "deposit", "usdc": "100"}}]
    assert accounting_events(data, NOW - 1000)["net_cash_flow_usdc"] == 100
