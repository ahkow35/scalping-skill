from datetime import datetime

import pytest

from account_api import AccountDataError
from account_risk import evaluate, failure_result
from test_account_observation import WALLET, snapshot, spot, stop


MIDNIGHT = int(datetime.fromisoformat("2026-09-07T00:00:10+08:00").timestamp() * 1000)
CONFIG = {"wallet": WALLET, "daily_loss_usdc": 100}


def observe(data, state=None, config=CONFIG):
    return evaluate(data, config, state, now_ms=data["asof_ms"])


def start_day():
    _, state = observe(snapshot(now=MIDNIGHT - 20_000))
    return observe(snapshot(now=MIDNIGHT), state)


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
