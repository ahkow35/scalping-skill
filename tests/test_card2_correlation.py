"""Correlation-aware aggregate-risk cap: cards on correlated coins collapse
to the single highest-net-R:R card so a scan never multiplies risk on one
thesis or shows an incoherent long-here/short-there fleet."""
import card2

BASE_CLOSES = [100 * (1.01 ** i) for i in range(25)]        # trending series
SCALED_CLOSES = [c * 2.0 for c in BASE_CLOSES]               # same log-returns
SAWTOOTH_CLOSES = [100 + (5 if i % 2 == 0 else -5) for i in range(25)]
FLAT_CLOSES = [100.0] * 25

_lvl = lambda p: {"price": p, "touches": 2, "kind": "x"}


def _long_read(coin, closes, t2=61.5, price_chg_pct=0.4, funding=0.01):
    return {"coin": coin, "mark": 58.8,
            "struct_15m": {"floors": [_lvl(57.1)],
                          "ceilings": [_lvl(60.0), _lvl(t2)],
                          "atr": 0.2, "sweep_reclaim": False,
                          "sweep_rejection": False},
            "struct_1h": {"floors": [_lvl(56.5)], "ceilings": [_lvl(t2)],
                         "atr": 0.6, "sweep_reclaim": False,
                         "sweep_rejection": False},
            "price_chg_pct": price_chg_pct, "oi_chg_pct": 1.0,
            "funding_8h_pct": funding, "flow_line": "flow n/a",
            "closes_15m": closes}


def _short_read(coin, closes, price_chg_pct=-0.4, funding=0.01):
    return {"coin": coin, "mark": 58.8,
            "struct_15m": {"floors": [_lvl(57.0)], "ceilings": [_lvl(60.0)],
                          "atr": 0.2, "sweep_reclaim": False,
                          "sweep_rejection": False},
            "struct_1h": {"floors": [_lvl(55.0)], "ceilings": [_lvl(61.0)],
                         "atr": 0.6, "sweep_reclaim": False,
                         "sweep_rejection": False},
            "price_chg_pct": price_chg_pct, "oi_chg_pct": 1.0,
            "funding_8h_pct": funding, "flow_line": "flow n/a",
            "closes_15m": closes}


PROF = {"equity": 5000.0, "phase": 1}
BEH = {"cooldown": {"active": False}}
OK = {"veto_long": False, "veto_short": False, "size_mult": 1.0, "flags": []}


def _build(reads):
    return card2.build_cards(reads, PROF, BEH, OK, [], 0)


# --- build_cards integration: cluster + collapse + suppression reason -----

def test_two_correlated_same_direction_collapse_to_higher_rr():
    hi = _long_read("HI", SCALED_CLOSES, t2=65.0)     # bigger reward -> higher rr
    lo = _long_read("LO", BASE_CLOSES, t2=61.5)
    out = _build([hi, lo])
    assert [c["coin"] for c in out["cards"]] == ["HI"]
    assert any("LO: correlated with HI" in r and "kept" in r
               for r in out["no_trade_reasons"])


def test_correlated_long_short_pair_keeps_only_higher_rr_side():
    long_card = _long_read("LONGC", SCALED_CLOSES)
    short_card = _short_read("SHORTC", BASE_CLOSES)
    out = _build([long_card, short_card])
    assert len(out["cards"]) == 1
    kept = out["cards"][0]["coin"]
    suppressed = "SHORTC" if kept == "LONGC" else "LONGC"
    assert any(f"{suppressed}: correlated with {kept}" in r
               for r in out["no_trade_reasons"])


def test_two_uncorrelated_coins_both_survive():
    a = _long_read("UNCA", BASE_CLOSES, t2=61.5)
    b = _long_read("UNCB", SAWTOOTH_CLOSES, t2=63.0)
    out = _build([a, b])
    assert {c["coin"] for c in out["cards"]} == {"UNCA", "UNCB"}
    assert not any("correlated with" in r for r in out["no_trade_reasons"])


def test_short_history_coin_fails_open_as_singleton():
    full = _long_read("FULL", BASE_CLOSES)
    short_hist = _long_read("SHORTHIST", SCALED_CLOSES[-10:], t2=63.0)
    out = _build([full, short_hist])
    assert {c["coin"] for c in out["cards"]} == {"FULL", "SHORTHIST"}
    assert not any("correlated with" in r for r in out["no_trade_reasons"])


def test_flat_series_fails_open_as_singleton():
    trending = _long_read("TREND", BASE_CLOSES)
    flat = _long_read("FLAT", FLAT_CLOSES, t2=63.0)
    out = _build([trending, flat])
    assert {c["coin"] for c in out["cards"]} == {"TREND", "FLAT"}
    assert not any("correlated with" in r for r in out["no_trade_reasons"])


def test_zero_and_one_card_pass_through_unchanged():
    out0 = _build([])
    assert out0["cards"] == [] and out0["no_trade_reasons"] == []

    out1 = _build([_long_read("SOLO", BASE_CLOSES)])
    assert [c["coin"] for c in out1["cards"]] == ["SOLO"]
    assert not any("correlated with" in r for r in out1["no_trade_reasons"])


def test_missing_closes_field_fails_open():
    read = _long_read("NOCLOSES", BASE_CLOSES)
    del read["closes_15m"]
    other = _long_read("OTHER", SCALED_CLOSES, t2=63.0)
    out = _build([read, other])
    assert {c["coin"] for c in out["cards"]} == {"NOCLOSES", "OTHER"}


# --- pure helper: _corr_clusters -------------------------------------------

def test_corr_clusters_groups_correlated_and_isolates_uncorrelated():
    cards = [{"coin": "A"}, {"coin": "B"}, {"coin": "C"}]
    closes_by_coin = {"A": BASE_CLOSES, "B": SCALED_CLOSES,
                      "C": SAWTOOTH_CLOSES}
    clusters = card2._corr_clusters(cards, closes_by_coin)
    assert sorted(sorted(c) for c in clusters) == [[0, 1], [2]]


def test_corr_clusters_fails_open_on_short_history():
    cards = [{"coin": "A"}, {"coin": "B"}]
    closes_by_coin = {"A": BASE_CLOSES, "B": SCALED_CLOSES[-10:]}
    clusters = card2._corr_clusters(cards, closes_by_coin)
    assert sorted(sorted(c) for c in clusters) == [[0], [1]]


def test_corr_clusters_fails_open_on_flat_series():
    cards = [{"coin": "A"}, {"coin": "B"}]
    closes_by_coin = {"A": BASE_CLOSES, "B": FLAT_CLOSES}
    clusters = card2._corr_clusters(cards, closes_by_coin)
    assert sorted(sorted(c) for c in clusters) == [[0], [1]]


def test_corr_clusters_empty_and_single():
    assert card2._corr_clusters([], {}) == []
    single = [{"coin": "A"}]
    assert card2._corr_clusters(single, {"A": BASE_CLOSES}) == [[0]]
