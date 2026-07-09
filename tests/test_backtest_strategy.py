# tests/test_backtest_strategy.py — strategy backtest (backtest.py), offline
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backtest

MS = 900_000


def c(h, l, cl, t):
    return {"o": cl, "h": h, "l": l, "c": cl, "v": 1000.0, "t": t}


def _setup_candles():
    """95<->110 triangle wave (tested ceiling ~110.4, tested floor ~94.7), then a
    sweep+rejection on the last warmup bar, then a decline to the floor — a
    clean short failed-breakout that clears the R:R floor and reaches target."""
    bars = []
    for i in range(60):
        ph = i % 20
        base = 95 + 1.5 * ph if ph <= 10 else 95 + 1.5 * (20 - ph)
        p = base + (0.1 * (i // 20) if base >= 110 else 0.0)
        bars.append((round(p + 0.3, 3), round(p - 0.3, 3), round(p, 3)))
    bars.append((110.6, 108.8, 109.0))  # sweep above ceiling, close back below
    for px in [107, 105, 103, 101, 99, 97, 95.2, 95, 95, 95]:
        bars.append((px + 0.3, px - 0.3, px))
    return [c(h, l, cl, i * MS) for i, (h, l, cl) in enumerate(bars)]


def _flat(n=80):
    return [c(105, 100, 102, i * MS) for i in range(n)]


def test_run_finds_passing_short_trade():
    trades = backtest.run("short", _setup_candles(), warmup=58, horizon=20, cooldown=2)
    assert len(trades) == 1
    tr = trades[0]
    assert tr["trigger"] == "short_A"
    assert tr["net_r"] > 0            # reaches target on the decline
    assert tr["status"] in ("t1_t2", "t1_be", "expired")


def test_run_empty_on_flat_tape():
    assert backtest.run("short", _flat(), warmup=40) == []
    assert backtest.run("long", _flat(), warmup=40) == []


def test_summarize_aggregates_by_trigger():
    trades = [
        {"trigger": "short_A", "net_r": 2.0, "ambiguous": False},
        {"trigger": "short_A", "net_r": -1.0, "ambiguous": True},
        {"trigger": "short_B", "net_r": 1.5, "ambiguous": False},
    ]
    rows = {r["trigger"]: r for r in backtest.summarize(trades)}
    assert rows["short_A"]["n"] == 2
    assert rows["short_A"]["win_rate"] == 50.0
    assert rows["short_A"]["expectancy_r"] == 0.5
    assert rows["short_A"]["ambiguous"] == 1
    assert rows["short_B"]["n"] == 1


def test_summarize_skips_none_net_r():
    assert backtest.summarize([{"trigger": "x", "net_r": None, "ambiguous": False}]) == []


def test_cooldown_suppresses_adjacent_refires():
    # a huge cooldown must collapse any run to at most one trade
    trades = backtest.run("short", _setup_candles(), warmup=58, horizon=20, cooldown=10_000)
    assert len(trades) <= 1


def test_pool_handles_empty():
    assert backtest._pool([])["n"] == 0


def test_rigor_lenses_execute():
    candles = _setup_candles()
    sw = backtest.sweep("short", candles, [0.25, 0.5], warmup=58, horizon=20, cooldown=2)
    assert [r["stop_buffer_atr"] for r in sw] == [0.25, 0.5]
    nz = backtest.noise("short", candles, levels=(0.0, 0.001), reps=2,
                        warmup=58, horizon=20, cooldown=2)
    assert nz[0]["noise"] == 0.0 and "expectancy_r" in nz[0]


def _aligned():
    """Aligned 5m+15m: 15m range (floor ~100 / ceiling ~110); on 5m price spikes
    above the ceiling and closes back below (failed breakout), then declines
    through the mid — a short_A setup."""
    c15 = []
    for k in range(80):
        ph = k % 20
        base = 100 + (ph if ph <= 10 else 20 - ph)
        p = base + (0.1 * (k // 20) if base >= 110 else 0.0)
        c15.append(c(round(p + 0.3, 3), round(p - 0.3, 3), round(p, 3), k * 900_000))
    c5 = []
    for m in range(240):
        k = m // 3
        ph = k % 20
        base = 100 + (ph if ph <= 10 else 20 - ph)
        p = base + (0.1 * (k // 20) if base >= 110 else 0.0)
        c5.append(c(round(p + 0.3, 3), round(p - 0.3, 3), round(p, 3), m * 300_000))
    tail = [(111.0, 108.5, 109.5), (109, 107, 108.0), (108, 105.5, 106.0),
            (106, 104, 104.5), (105, 103, 103.5), (104, 102, 103.0)]
    for j, (h, l, cl) in enumerate(tail):
        idx = 240 - len(tail) + j
        c5[idx] = c(h, l, cl, idx * 300_000)
    return c5, c15


def test_run_intraday_finds_trade():
    c5, c15 = _aligned()
    trades = backtest.run_intraday("short", c5, c15, warmup5=200, cooldown=2)
    assert len(trades) >= 1


def test_run_intraday_allow_filter_restricts_triggers():
    c5, c15 = _aligned()
    trades = backtest.run_intraday("short", c5, c15, warmup5=200, cooldown=2, allow=("A",))
    assert all(t["trigger"] == "short_A" for t in trades)


def test_run_intraday_retest_mode_runs():
    c5, c15 = _aligned()
    trades = backtest.run_intraday("short", c5, c15, warmup5=200, cooldown=2,
                                   allow=("A",), trig_params={"entry_mode": "retest"})
    assert all(t["trigger"] == "short_A" for t in trades)


def test_run_intraday_empty_on_flat():
    c5 = [c(105, 100, 102, i * 300_000) for i in range(240)]
    c15 = [c(105, 100, 102, i * 900_000) for i in range(80)]
    assert backtest.run_intraday("long", c5, c15, warmup5=200) == []
