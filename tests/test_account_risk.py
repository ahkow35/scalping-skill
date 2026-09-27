from datetime import datetime

import pytest

from account_api import AccountDataError
from account_risk import evaluate, failure_result, validate_config
from test_account_observation import WALLET, snapshot, spot, stop


MIDNIGHT = int(datetime.fromisoformat("2026-09-07T00:00:10+08:00").timestamp() * 1000)
CONFIG = {"wallet": WALLET, "daily_loss_usdc": 100}


def observe(data, state=None, config=CONFIG):
    return evaluate(data, config, state, now_ms=data["asof_ms"])


def start_day():
    _, state = observe(snapshot(now=MIDNIGHT - 20_000))
    return observe(snapshot(now=MIDNIGHT), state)


def fill(tid, time, side, sz, px, start_position):
    return {"coin": "HYPE", "tid": tid, "time": time, "side": side, "sz": str(sz), "px": str(px),
            "startPosition": str(start_position), "closedPnl": "0", "fee": "0", "feeToken": "USDC"}


def underwater_long_fills():
    """Open 5 @ 100 from flat, then add 5 @ 90 — worse than the running entry."""
    return [fill(101, MIDNIGHT - 10_000, "B", 5, 100, 0), fill(102, MIDNIGHT + 500, "B", 5, 90, 5)]


def underwater_short_fills():
    """Open 5 @ 100 from flat, then add 5 @ 110 — worse than the running entry."""
    return [fill(201, MIDNIGHT - 10_000, "A", 5, 100, 0), fill(202, MIDNIGHT + 500, "A", 5, 110, -5)]


def test_first_midday_observation_cannot_invent_start_of_day_equity():
    report, _ = observe(snapshot(now=MIDNIGHT + 3_600_000))
    assert report["status"] == "WARMUP"
    assert not report["entry_allowed"]


def test_open_loss_trips_and_recovery_and_restart_do_not_clear_latch():
    report, state = start_day()
    assert report["status"] == "CLEAR"
    report, state = observe(snapshot(now=MIDNIGHT + 1000, equity=880, upnl=-120,
                                     size=10, orders=[stop(trigger=80)]), state)
    assert report["status"] == "HALT"
    report, state = observe(snapshot(now=MIDNIGHT + 2000), state)
    assert report["status"] == "HALT"
    assert failure_result("network down", state)["status"] == "HALT"


def test_deposit_cannot_hide_trading_loss():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=1380, upnl=-120, size=10)
    data["ledger"] = [{"time": MIDNIGHT + 500, "delta": {"type": "deposit", "usdc": "500"}}]
    report, _ = observe(data, state)
    assert report["daily"]["net_pnl_usdc"] == -120
    assert report["status"] == "HALT"


def test_withdrawal_is_not_a_trading_loss():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=800)
    data["ledger"] = [{"time": MIDNIGHT + 500, "delta": {"type": "withdraw", "usdc": "200"}}]
    report, _ = observe(data, state)
    assert report["status"] == "CLEAR"
    assert report["daily"]["net_pnl_usdc"] == 0


def test_unexplained_equity_change_fails_closed_without_resetting_baseline():
    _, state = start_day()
    report, retained = observe(snapshot(now=MIDNIGHT + 1000, equity=1200), state)
    assert report["status"] == "DATA_UNAVAILABLE"
    assert retained == state


def test_new_day_preserves_loss_across_midnight_observation_interval():
    before = MIDNIGHT - 20_000
    _, state = observe(snapshot(now=before))
    report, _ = observe(snapshot(now=MIDNIGHT, equity=880, upnl=-120, size=10), state)
    assert report["status"] == "HALT"
    assert report["daily"]["baseline_at_ms"] == before


def test_missing_stops_and_excess_open_risk_block_entries():
    report, _ = observe(snapshot(now=MIDNIGHT, size=10))
    assert report["status"] == "UNPROTECTED"
    report, _ = observe(snapshot(now=MIDNIGHT, size=10, orders=[stop(trigger=80)]))
    assert report["status"] == "EXCESS_OPEN_RISK"
    assert not report["entry_allowed"]


def test_limit_change_cannot_reset_same_day_risk_budget():
    _, state = start_day()
    report, _ = observe(snapshot(now=MIDNIGHT + 1000), state,
                        {"wallet": WALLET, "daily_loss_usdc": 500})
    assert report["daily"]["limit_usdc"] == 100


@pytest.mark.parametrize("state", [{}, {"version": 1, "day": []}])
def test_corrupt_state_is_not_treated_as_first_run(state):
    with pytest.raises(AccountDataError):
        observe(snapshot(now=MIDNIGHT), state)


def test_first_near_midnight_check_cannot_hide_prebaseline_loss():
    data = snapshot(now=MIDNIGHT, equity=880)
    data["fills"] = [{"coin": "HYPE", "tid": 1, "time": MIDNIGHT - 5000,
                      "closedPnl": "-120", "fee": "0", "feeToken": "USDC"}]
    report, _ = observe(data)
    assert report["status"] == "WARMUP"
    assert not report["entry_allowed"]


def test_unified_account_day_reconciles_spot_usdc_against_perp_fills():
    first = snapshot(now=MIDNIGHT - 20_000, mode="unifiedAccount", equity=0)
    first["spot"] = spot(usdc="7000")
    report, state = observe(first)
    assert report["status"] == "WARMUP" and report["observation"]["equity_usdc"] == 7000
    later = snapshot(now=MIDNIGHT, mode="unifiedAccount", equity=0, size=10, upnl=-2,
                     orders=[stop(trigger=97)])
    later["spot"] = spot(usdc="6996.5")
    later["fills"] = [{"coin": "HYPE", "tid": 1, "time": MIDNIGHT - 10_000, "feeToken": "USDC",
                       "closedPnl": "-3", "fee": "0.5"}]
    report, state = observe(later, state)
    assert report["status"] == "CLEAR"
    assert report["daily"]["net_pnl_usdc"] == pytest.approx(-5.5)
    assert report["daily"]["reconciliation_residual_usdc"] == pytest.approx(0)


def test_oversized_position_adds_a_reason_naming_coin_notional_and_cap():
    config = {**CONFIG, "max_position_notional_usdc": 500}
    report, _ = observe(snapshot(now=MIDNIGHT, size=10), config=config)
    reasons = " ".join(report["reasons"])
    assert "HYPE" in reasons and "1,000.00" in reasons and "500.00" in reasons and "exceeds size cap" in reasons


def test_position_under_cap_adds_no_oversized_reason():
    config = {**CONFIG, "max_position_notional_usdc": 5000}
    report, _ = observe(snapshot(now=MIDNIGHT, size=10), config=config)
    assert not any("exceeds size cap" in reason for reason in report["reasons"])
    assert not any("not configured" in reason for reason in report["reasons"])


def test_missing_cap_adds_a_not_configured_reason():
    report, _ = observe(snapshot(now=MIDNIGHT, size=10), config=CONFIG)
    assert any("not configured" in reason for reason in report["reasons"])


def test_old_config_without_cap_field_keeps_working():
    result = validate_config({"wallet": WALLET, "daily_loss_usdc": 100})
    assert result["max_position_notional_usdc"] is None


def test_add_to_a_losing_long_adds_a_reason_naming_coin_time_and_price():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10, orders=[stop(trigger=95)])
    data["fills"] = underwater_long_fills()
    report, _ = observe(data, state)
    assert report["status"] == "CLEAR"
    reasons = " ".join(report["reasons"])
    assert "HYPE" in reasons and "90" in reasons and "added to a losing long" in reasons


def test_add_to_a_losing_short_adds_a_reason_naming_coin_time_and_price():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=-10, orders=[stop(trigger=105, side="B")])
    data["fills"] = underwater_short_fills()
    report, _ = observe(data, state)
    assert report["status"] == "CLEAR"
    reasons = " ".join(report["reasons"])
    assert "HYPE" in reasons and "110" in reasons and "added to a losing short" in reasons


def test_add_to_a_winning_position_adds_no_warning():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10, orders=[stop(trigger=95)])
    data["fills"] = [fill(301, MIDNIGHT - 10_000, "B", 5, 100, 0), fill(302, MIDNIGHT + 500, "B", 5, 110, 5)]
    report, _ = observe(data, state)
    assert not any("added to a losing" in reason for reason in report["reasons"])


def test_fresh_open_from_flat_adds_no_warning():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=990, upnl=-10, size=10, orders=[stop(trigger=95)])
    data["fills"] = [fill(401, MIDNIGHT + 500, "B", 10, 100, 0)]
    report, _ = observe(data, state)
    assert not any("added to a losing" in reason for reason in report["reasons"])


def test_close_then_reopen_then_profitable_add_produces_no_false_warning():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=995, upnl=-5, size=15)
    data["fills"] = [
        fill(501, MIDNIGHT - 15_000, "B", 10, 100, 0),    # open long 10 @ 100
        fill(502, MIDNIGHT - 12_000, "A", 10, 100, 10),   # close flat @ 100
        fill(503, MIDNIGHT - 8_000, "B", 10, 80, 0),      # reopen long 10 @ 80
        fill(504, MIDNIGHT + 200, "B", 5, 90, 10),        # add @ 90 — profitable vs the 80 reopen entry
    ]
    report, _ = observe(data, state)
    assert not any("added to a losing" in reason for reason in report["reasons"])


def test_spot_fills_do_not_trigger_a_perp_underwater_warning():
    _, state = start_day()
    data = snapshot(now=MIDNIGHT + 1000, equity=1000, upnl=0, size=0)
    data["fills"] = [
        {"coin": "@107", "tid": 601, "time": MIDNIGHT - 10_000, "side": "B", "sz": "10", "px": "100",
         "startPosition": "10", "dir": "Buy", "closedPnl": "0", "fee": "0", "feeToken": "USDC"},
        {"coin": "@107", "tid": 602, "time": MIDNIGHT + 200, "side": "B", "sz": "10", "px": "90",
         "startPosition": "20", "dir": "Buy", "closedPnl": "0", "fee": "0", "feeToken": "USDC"},
    ]
    report, _ = observe(data, state)
    assert not any("added to a losing" in reason for reason in report["reasons"])


def test_oversized_and_underwater_add_warnings_do_not_change_status_or_entry_allowed():
    _, state = start_day()
    config = {**CONFIG, "max_position_notional_usdc": 500}
    data = snapshot(now=MIDNIGHT + 1000, equity=970, upnl=-30, size=10, orders=[stop(trigger=95)])
    data["fills"] = underwater_long_fills()
    report, _ = observe(data, state, config)
    assert report["status"] == "CLEAR"
    assert report["entry_allowed"] is True
    reasons = " ".join(report["reasons"])
    assert "exceeds size cap" in reasons
    assert "added to a losing long" in reasons
