import pytest

from account_api import AccountDataError, fetch_history, fetch_snapshot, post_info, validate_wallet


WALLET = "0x" + "1" * 40
NOW = 1_800_000_000_000


def test_transport_cannot_send_trading_actions():
    with pytest.raises(AccountDataError, match="read-only"):
        post_info({"type": "order", "orders": []})


@pytest.mark.parametrize("wallet", ["secret", "0x123", "0x" + "0" * 40, None])
def test_invalid_wallet_rejected(wallet):
    with pytest.raises(AccountDataError):
        validate_wallet(wallet)


def test_history_preserves_events_at_split_boundary_and_deduplicates():
    rows = [{"time": stamp, "id": stamp} for stamp in range(1000)]
    calls = []

    def exchange(payload):
        calls.append(payload)
        matching = [row for row in rows if payload["startTime"] <= row["time"] <= payload["endTime"]]
        return matching[:500]

    actual = fetch_history("userFunding", WALLET, 0, 999, post=exchange)
    assert actual == rows
    assert len(calls) > 1


def test_saturated_single_millisecond_is_unknown_not_complete():
    with pytest.raises(AccountDataError, match="single timestamp"):
        fetch_history("userFunding", WALLET, 100, 100,
                      post=lambda payload: [{"time": 100, "id": n} for n in range(500)])


def test_history_rejects_event_outside_requested_window():
    with pytest.raises(AccountDataError, match="outside"):
        fetch_history("userFunding", WALLET, 100, 200, post=lambda payload: [{"time": 201}])


def _exchange(payload):
    kind = payload["type"]
    if kind == "userRole":
        return {"role": "user"}
    if kind == "userAbstraction":
        return "disabled"
    if kind == "perpDexs":
        return [None, {"name": "xyz"}]
    if kind == "clearinghouseState":
        return {"time": NOW, "marginSummary": {"accountValue": "100"}, "assetPositions": []}
    if kind == "meta":
        return {"collateralToken": 0}
    return []


def test_snapshot_reads_each_venue_and_all_history_without_signing():
    calls = []

    def exchange(payload):
        calls.append(payload)
        return _exchange(payload)

    result = fetch_snapshot(WALLET, NOW - 1000, post=exchange, clock=lambda: NOW)
    assert set(result["venues"]) == {"", "xyz"}
    assert result["asof_ms"] == NOW
    assert result["history_start_ms"] == NOW - 1000
    assert {payload["dex"] for payload in calls if payload["type"] == "frontendOpenOrders"} == {"", "xyz"}
    assert {payload["type"] for payload in calls} <= {
        "userRole", "userAbstraction", "perpDexs", "meta", "clearinghouseState",
        "frontendOpenOrders", "userFillsByTime", "userFunding", "userNonFundingLedgerUpdates",
    }


def test_missing_venue_does_not_return_partial_account_as_clear():
    def exchange(payload):
        if payload["type"] == "frontendOpenOrders" and payload["dex"] == "xyz":
            raise AccountDataError("venue unavailable")
        return _exchange(payload)

    with pytest.raises(AccountDataError, match="venue unavailable"):
        fetch_snapshot(WALLET, NOW - 1000, post=exchange, clock=lambda: NOW)


def test_api_agent_address_is_not_treated_as_an_empty_trading_account():
    with pytest.raises(AccountDataError, match="API-agent"):
        fetch_snapshot(WALLET, NOW - 1000,
                       post=lambda payload: {"role": "agent"}, clock=lambda: NOW)
