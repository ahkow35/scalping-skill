import card2


def _lvl(p, touches=2):
    return {"price": p, "touches": touches, "kind": "x"}


S15 = {"floors": [_lvl(57.1)], "ceilings": [_lvl(60.0), _lvl(61.5)],
       "floor_below": _lvl(57.1), "ceiling_above": _lvl(60.0),
       "atr": 0.20, "sweep_reclaim": False, "sweep_rejection": False}
S1H = {"floors": [_lvl(56.5), _lvl(54.0)], "ceilings": [_lvl(61.5)],
       "floor_below": _lvl(56.5), "ceiling_above": _lvl(61.5),
       "atr": 0.60, "sweep_reclaim": False, "sweep_rejection": False}


def test_lean_long_on_price_up_oi_up():
    assert card2.derive_lean(0.4, 1.2, S15) == "long"


def test_lean_short_on_price_down_oi_up():
    assert card2.derive_lean(-0.4, 1.2, S15) == "short"


def test_lean_none_when_flat():
    assert card2.derive_lean(0.02, -0.1, S15) is None


def test_sweep_reclaim_overrides_to_long():
    s = dict(S15, sweep_reclaim=True)
    assert card2.derive_lean(-0.5, 0.5, s) == "long"


def test_derive_setup_long_uses_structural_stop_below_entry_level():
    setup = card2.derive_setup("long", S15, S1H, mark=58.8)
    assert setup is not None
    assert setup["entry"] == 57.1                       # retest of 15m floor
    lo, hi = setup["zone"]
    assert lo < 57.1 < hi
    # stop = 1h floor below entry (56.5) minus 0.25*atr1h buffer
    assert abs(setup["stop"] - (56.5 - 0.25 * 0.60)) < 1e-9
    assert setup["t1"] == 60.0 and setup["t2"] == 61.5


def test_derive_setup_none_when_no_structural_stop_within_3pct():
    s1h = dict(S1H, floors=[_lvl(40.0)], floor_below=_lvl(40.0))
    assert card2.derive_setup("long", S15, s1h, mark=58.8) is None


def test_rr_net_charges_fees_both_sides():
    # long: entry 100, stop 98, t1 103, t2 105 -> gross reward 4, risk 2
    gross = (0.5 * 3 + 0.5 * 5) / 2.0
    net = card2.rr_net("long", 100.0, 98.0, 103.0, 105.0)
    assert net < gross
    fee_rt = (card2.FEE_MAKER_PCT + card2.FEE_TAKER_PCT) / 100 * 100.0
    expect = (4.0 - fee_rt) / (2.0 + fee_rt)
    assert abs(net - expect) < 1e-9


def test_size_from_risk():
    out = card2.size_from_risk(5000.0, 0.5, entry=57.1, stop=56.0)
    stop_frac = (57.1 - 56.0) / 57.1
    assert abs(out["notional_usdc"] - 5000.0 * 0.005 / stop_frac) < 0.01
    assert abs(out["margin_usdc"] - out["notional_usdc"] / 10) < 0.01


S15_SHORT = {"floors": [_lvl(60.0), _lvl(58.5)], "ceilings": [_lvl(62.9)],
             "floor_below": _lvl(60.0), "ceiling_above": _lvl(62.9),
             "atr": 0.20, "sweep_reclaim": False, "sweep_rejection": False}
S1H_SHORT = {"floors": [_lvl(57.0)], "ceilings": [_lvl(63.5), _lvl(66.0)],
             "floor_below": _lvl(57.0), "ceiling_above": _lvl(63.5),
             "atr": 0.60, "sweep_reclaim": False, "sweep_rejection": False}


def test_derive_setup_short_mirrors_long_using_ceilings_as_entry():
    setup = card2.derive_setup("short", S15_SHORT, S1H_SHORT, mark=61.2)
    assert setup is not None
    assert setup["entry"] == 62.9                       # retest of 15m ceiling
    lo, hi = setup["zone"]
    assert lo < 62.9 < hi
    # stop = 1h ceiling above entry (63.5) plus 0.25*atr1h buffer
    assert abs(setup["stop"] - (63.5 + 0.25 * 0.60)) < 1e-9
    assert setup["t1"] == 60.0 and setup["t2"] == 58.5
