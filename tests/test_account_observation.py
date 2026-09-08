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


@pytest.mark.parametrize("mode", ["unifiedAccount", "portfolioMargin", "dexAbstraction", "default", None])
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
