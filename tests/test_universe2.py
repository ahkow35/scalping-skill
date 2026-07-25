import math
import universe2


def _meta(rows):
    # rows: list of (name, oi_usdc, day_vol, mark)
    universe = [{"name": n} for n, *_ in rows]
    ctxs = [{"openInterest": str(oi / mark), "dayNtlVlm": str(dv),
             "markPx": str(mark)} for _, oi, dv, mark in rows]
    return [{"universe": universe}, ctxs]


def _candles(rets):
    # build 1h candles with given close-to-close log returns
    px, out = 100.0, []
    for i, r in enumerate([0.0] + rets):
        px *= math.exp(r)
        out.append({"t": i * 3600_000, "o": str(px), "h": str(px),
                    "l": str(px), "c": str(px), "v": "1", "n": 10})
    return out


def test_realized_vol_sums_abs_log_returns():
    v = universe2.realized_vol_24h(_candles([0.01, -0.02, 0.005]))
    assert abs(v - 0.035) < 1e-9


def test_select_universe_hype_first_btc_excluded_floors_applied():
    rows = [
        ("BTC",  2_400_000_000, 1_800_000_000, 65000.0),
        ("HYPE", 1_300_000_000,   280_000_000, 58.0),
        ("SOL",    350_000_000,   110_000_000, 75.0),
        ("WIF",     60_000_000,    30_000_000, 2.0),
        ("DUST",     1_000_000,       500_000, 0.1),   # fails floors
    ]
    vols = {"SOL": [0.001] * 24, "WIF": [0.01] * 24, "HYPE": [0.005] * 24}

    def fake_fetch(coin, interval, s, e):
        return _candles(vols[coin])

    got = universe2.select_universe(0, meta=_meta(rows), candle_fetch=fake_fetch)
    assert got[0] == "HYPE"
    assert "BTC" not in got and "DUST" not in got
    # WIF (vol 0.24) ranks above SOL (0.024)
    assert got[1] == "WIF"
