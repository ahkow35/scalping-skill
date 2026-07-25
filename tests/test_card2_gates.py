import card2


def _btc_struct(classification):
    return {"classification": classification, "floors": [], "ceilings": [],
            "floor_below": None, "ceiling_above": None, "atr": 300.0,
            "sweep_reclaim": False, "sweep_rejection": False}


def _btc_candles(last_range_mult=1.0, n=30):
    # 1h candles; last bar's range = mult * average of prior ranges
    base = [{"t": i, "o": "65000", "h": "65100", "l": "64900",
             "c": "65000", "v": "10", "n": 100} for i in range(n - 1)]
    span = 100 * last_range_mult
    base.append({"t": n, "o": "65000", "h": str(65000 + span),
                 "l": str(65000 - span), "c": "64950", "v": "10", "n": 100})
    return base


def test_funding_extreme_vetoes_crowded_side():
    g = card2.macro_gate(_btc_struct("range"), _btc_candles(), 0.05)
    assert g["veto_long"] and not g["veto_short"]
    g = card2.macro_gate(_btc_struct("range"), _btc_candles(), -0.05)
    assert g["veto_short"] and not g["veto_long"]


def test_btc_breakdown_vetoes_longs():
    g = card2.macro_gate(_btc_struct("breakdown"), _btc_candles(), 0.0)
    assert g["veto_long"] and not g["veto_short"]


def test_atr_spike_halves_size_not_veto():
    g = card2.macro_gate(_btc_struct("range"), _btc_candles(2.5), 0.0)
    assert g["size_mult"] == 0.5 and not g["veto_long"]


def _entry(ts_ms, r=None):
    e = {"ts_ms": ts_ms, "mode": "ENTRY", "system": "scan2", "outcome": None}
    if r is not None:
        e["outcome"] = {"outcome_r": r, "resolved_at_ms": ts_ms + 1000}
    return e


def test_session_stop_after_two_recent_losses():
    now = 100 * 3600_000
    recent = [_entry(now - 3600_000, -1.0), _entry(now - 7200_000, -0.5)]
    assert card2.session_stop_active(recent, now)
    old = [_entry(now - 13 * 3600_000, -1.0), _entry(now - 3600_000, -0.5)]
    assert not card2.session_stop_active(old, now)


def test_risk_pct_stays_half_until_40_positive_resolved():
    losses = [_entry(i, -0.2) for i in range(45)]
    assert card2.v2_risk_pct(losses) == 0.5          # negative expectancy
    wins = [_entry(i, 0.5) for i in range(39)]
    assert card2.v2_risk_pct(wins) == 0.5            # not enough samples
    assert card2.v2_risk_pct(wins + [_entry(99, 0.5)]) == 1.0


def test_build_cards_suppresses_low_rr_and_respects_veto(monkeypatch):
    lvl = lambda p: {"price": p, "touches": 2, "kind": "x"}
    read = {"coin": "HYPE", "mark": 58.8,
            "struct_15m": {"floors": [lvl(57.1)],
                           "ceilings": [lvl(60.0), lvl(61.5)],
                           "atr": 0.2, "sweep_reclaim": False,
                           "sweep_rejection": False},
            "struct_1h": {"floors": [lvl(56.5)], "ceilings": [lvl(61.5)],
                          "atr": 0.6, "sweep_reclaim": False,
                          "sweep_rejection": False},
            "price_chg_pct": 0.4, "oi_chg_pct": 1.0,
            "funding_8h_pct": 0.01, "flow_line": "flow n/a"}
    prof = {"equity": 5000.0, "phase": 1}
    beh = {"cooldown": {"active": False}}
    ok = {"veto_long": False, "veto_short": False, "size_mult": 1.0,
          "flags": []}
    out = card2.build_cards([read], prof, beh, ok, [], 0)
    assert len(out["cards"]) == 1
    card = out["cards"][0]
    assert card["side"] == "long" and card["rr"] >= card2.RR_FLOOR
    assert card["risk_pct"] == 0.5

    vetoed = dict(ok, veto_long=True)
    out2 = card2.build_cards([read], prof, beh, vetoed, [], 0)
    assert out2["cards"] == []
    assert any("veto" in r for r in out2["no_trade_reasons"])
