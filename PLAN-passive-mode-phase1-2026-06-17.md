# Passive Mode — Phase 1 (Regime Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic `regime` classifier to the scalp data layer and surface a read-only "market weather" read, then backtest-validate its thresholds — the §7 gate that must pass before any passive-trade machinery is built.

**Architecture:** A new pure module `regime.py` computes a regime dict (range compression, BTC correlation, two-sided-flow score, directionality, label, coarse `fade_ok`) from data `fetch_market.py` already gathers. `fetch_market.py` fetches BTC 15m candles (needed for correlation), imports `regime`, and attaches `out["regime"]`. `backtest_thresholds.py` gains a routine that scores regime labels against forward outcomes and sweeps the key threshold for a plateau. `scalp-core.md` gets a read-only weather line. No trade behavior changes in this phase.

**Tech Stack:** Python 3 stdlib only (no deps), pytest, Hyperliquid REST (`candleSnapshot`), existing scalp repo conventions.

**Scope boundary:** This plan ships the regime READ only. The trade-grade edge-proximity `fade_ok` guard, the passive-fade module, expectancy sizing, behavioral carve-out, and replay support are **Plan 2**, authored after this plan's backtest (Task 6) yields validated thresholds. See spec §8.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `regime.py` | Pure regime classifier — candles+flow+book → regime dict. No I/O. | Create |
| `tests/test_regime.py` | Unit tests for every classifier function with deterministic inputs. | Create |
| `fetch_market.py` | Fetch BTC 15m candles; call `regime.classify`; attach `out["regime"]`. | Modify |
| `backtest_thresholds.py` | Validate regime thresholds vs forward outcomes + plateau sweep. | Modify |
| `scalp-core.md` | Read-only "market weather" line in QUICK/TINY output. | Modify |

All thresholds live in one `DEFAULT_PARAMS` dict in `regime.py` and are **initial candidates** confirmed/retuned by Task 6.

---

## Task 1: Regime primitives — true range, range compression, directionality

**Files:**
- Create: `regime.py`
- Test: `tests/test_regime.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_regime.py
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import regime


def c(o, h, l, cl):
    return {"o": o, "h": h, "l": l, "c": cl}


def test_true_ranges_uses_prev_close():
    candles = [c(100, 101, 99, 100), c(100, 103, 99, 102)]
    # bar0 has no prev close -> high-low = 2.0
    # bar1: max(103-99, |103-100|, |99-100|) = 4.0
    assert regime.true_ranges(candles) == [2.0, 4.0]


def test_range_compression_quiet_when_recent_smaller():
    # 20 wide bars (range 4) then 6 tight bars (range 1) -> ratio < 1
    wide = [c(100, 102, 98, 100) for _ in range(20)]
    tight = [c(100, 100.5, 99.5, 100) for _ in range(6)]
    ratio = regime.range_compression(wide + tight, recent_n=6, baseline_n=20)
    assert ratio < 0.5


def test_range_compression_none_when_insufficient_bars():
    assert regime.range_compression([c(100, 101, 99, 100)], recent_n=6, baseline_n=20) is None


def test_directionality_high_for_trend():
    # monotonic up: net move == sum of abs moves -> directionality ~1.0
    candles = [c(0, 0, 0, float(i)) for i in range(10)]
    assert regime.directionality(candles) > 0.95


def test_directionality_low_for_chop():
    # oscillating closes: net ~0, large path -> directionality ~0
    closes = [100, 102, 100, 102, 100, 102, 100, 102]
    candles = [c(0, 0, 0, x) for x in closes]
    assert regime.directionality(candles) < 0.2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'regime'`

- [ ] **Step 3: Write minimal implementation**

```python
# regime.py
"""Deterministic market-regime classifier for the scalp skill.

Pure functions over already-fetched data (candles, taker_delta, book). No I/O,
no network — fetch_market.py calls classify() and attaches the result. Mirrors
the taker_delta / microprice pattern: data layer computes, the LLM reads.

Phase 1 ships the READ (regime label + coarse fade_ok). The trade-grade
edge-proximity fade_ok guard and the passive-fade module are Plan 2.
"""


def true_ranges(candles):
    """Wilder true range per bar. First bar uses high-low (no prior close)."""
    trs = []
    prev_close = None
    for k in candles:
        hi, lo = float(k["h"]), float(k["l"])
        if prev_close is None:
            trs.append(round(hi - lo, 10))
        else:
            trs.append(round(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)), 10))
        prev_close = float(k["c"])
    return trs


def range_compression(candles, recent_n=6, baseline_n=20):
    """recent avg TR / baseline avg TR. <1 compressing (quiet), >1 expanding.

    None if fewer than baseline_n bars (not enough history to judge)."""
    if len(candles) < baseline_n or recent_n > baseline_n:
        return None
    trs = true_ranges(candles)
    recent = trs[-recent_n:]
    baseline = trs[-baseline_n:]
    base_avg = sum(baseline) / len(baseline)
    if base_avg <= 0:
        return None
    return round((sum(recent) / len(recent)) / base_avg, 3)


def directionality(candles):
    """|net close move| / sum|bar-to-bar close move| over the series, in [0,1].

    ~1 = clean trend (all moves same direction); ~0 = chop (path cancels out).
    None if fewer than 2 closes or a degenerate flat path."""
    if len(candles) < 2:
        return None
    closes = [float(k["c"]) for k in candles]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path <= 0:
        return None
    return round(net / path, 3)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add regime.py tests/test_regime.py
git commit -m "feat(regime): true range, range compression, directionality primitives"
```

---

## Task 2: BTC correlation + two-sided-flow score

**Files:**
- Modify: `regime.py`
- Test: `tests/test_regime.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_regime.py

def test_btc_corr_perfect_positive():
    # identical return shapes -> corr ~ +1
    prim = [c(0, 0, 0, x) for x in [100, 101, 102, 101, 103]]
    btc = [c(0, 0, 0, x) for x in [200, 202, 204, 202, 206]]
    assert regime.btc_corr(prim, btc, lookback=5) > 0.95


def test_btc_corr_none_when_too_short():
    prim = [c(0, 0, 0, 100), c(0, 0, 0, 101)]
    btc = [c(0, 0, 0, 200), c(0, 0, 0, 202)]
    assert regime.btc_corr(prim, btc, lookback=20) is None


def test_two_sided_flow_high_when_balanced():
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 52.0},
          "1h": {"buy_share_pct": 48.0}}
    assert regime.two_sided_flow(td) > 0.9


def test_two_sided_flow_low_when_one_sided():
    td = {"5m": {"buy_share_pct": 2.0}, "15m": {"buy_share_pct": 5.0},
          "1h": {"buy_share_pct": 0.0}}
    assert regime.two_sided_flow(td) < 0.1


def test_two_sided_flow_none_when_no_volume():
    td = {"5m": {"buy_share_pct": None}, "15m": {"buy_share_pct": None}}
    assert regime.two_sided_flow(td) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: FAIL — `AttributeError: module 'regime' has no attribute 'btc_corr'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to regime.py

def _returns(candles):
    closes = [float(k["c"]) for k in candles]
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] != 0]


def btc_corr(primary_candles, btc_candles, lookback=32):
    """Pearson correlation of primary vs BTC returns over the last `lookback`
    aligned bars. None if either series is too short. Assumes both candle lists
    are the same interval and end at the same time (caller aligns by fetching
    matching windows)."""
    n = min(len(primary_candles), len(btc_candles))
    if n < lookback + 1:
        return None
    p = _returns(primary_candles[-(lookback + 1):])
    b = _returns(btc_candles[-(lookback + 1):])
    m = min(len(p), len(b))
    if m < 3:
        return None
    p, b = p[-m:], b[-m:]
    mp, mb = sum(p) / m, sum(b) / m
    cov = sum((p[i] - mp) * (b[i] - mb) for i in range(m))
    vp = sum((p[i] - mp) ** 2 for i in range(m))
    vb = sum((b[i] - mb) ** 2 for i in range(m))
    if vp <= 0 or vb <= 0:
        return None
    return round(cov / (vp ** 0.5 * vb ** 0.5), 3)


def two_sided_flow(taker_delta, windows=("5m", "15m", "1h")):
    """Score in [0,1]: 1 = aggressor flow perfectly balanced near 50/50
    (mean-reverting tape), 0 = fully one-sided (trending). None if no window
    has volume (all buy_share_pct None)."""
    devs = []
    for w in windows:
        bucket = taker_delta.get(w) if isinstance(taker_delta, dict) else None
        if not isinstance(bucket, dict):
            continue
        bs = bucket.get("buy_share_pct")
        if bs is None:
            continue
        devs.append(abs(float(bs) - 50.0) / 50.0)
    if not devs:
        return None
    return round(1.0 - sum(devs) / len(devs), 3)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add regime.py tests/test_regime.py
git commit -m "feat(regime): BTC return correlation + two-sided-flow score"
```

---

## Task 3: `classify()` — compose primitives into label + coarse fade_ok

**Files:**
- Modify: `regime.py`
- Test: `tests/test_regime.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_regime.py
# cc(close, rng): a candle centered on `close` with total high-low range `rng`.
# (The existing c(o,h,l,cl) helper is at the top of the file; reuse it.)


def cc(close, rng):
    return c(close, close + rng / 2, close - rng / 2, close)


def test_classify_trending_label():
    # clean uptrend -> directionality ~1.0 -> trending (even though BTC is also
    # trending and correlated, the trending rule wins because dir > 0.5).
    up = [cc(100 + i, 1.0) for i in range(48)]
    btc = [cc(200 + i, 1.0) for i in range(48)]
    td = {"5m": {"buy_share_pct": 95.0}, "15m": {"buy_share_pct": 92.0}}
    book = {"execution": {"microprice_dev_bps": 1.0}}
    out = regime.classify({"15m": up, "1h": up}, {"15m": btc}, td, book)
    assert out["regime_label"] == "trending"
    assert out["fade_ok"] is False


def test_classify_ranging_label_sets_fade_ok():
    # bounded oscillation + balanced flow + BTC flat (corr undefined -> the
    # correlated-chop rule is skipped) -> ranging, and a calm book -> fade_ok.
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 51.0},
          "1h": {"buy_share_pct": 49.0}}
    book = {"execution": {"microprice_dev_bps": 0.2}}
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "ranging"
    assert out["fade_ok"] is True


def test_classify_correlated_chop_label():
    # bounded oscillation but BTC oscillates IN PHASE -> corr ~ +1, low
    # directionality -> correlated-chop (sit out). fade_ok must be False.
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200 + (i % 2), 1.0) for i in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": 0.2}}
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "correlated-chop"
    assert out["fade_ok"] is False


def test_classify_quiet_label_when_recent_compressed():
    # 42 wide oscillating bars then 6 compressed bars -> range_compression < 0.6
    # with low (but defined) directionality -> quiet. BTC irrelevant (quiet rule
    # is evaluated before correlation).
    wide = [cc(100 + (i % 2) * 3.0, 4.0) for i in range(42)]
    tight = [cc(100 + (i % 2) * 0.1, 0.2) for i in range(6)]
    series = wide + tight
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": 0.0}}
    out = regime.classify({"15m": series, "1h": series}, {"15m": btc}, td, book)
    assert out["regime_label"] == "quiet"
    assert out["fade_ok"] is False


def test_classify_fade_ok_false_when_book_leans_hard():
    # ranging fixture (flat BTC) so the label IS ranging -> isolates the book
    # guard: a hard one-sided book (|dev| > book_lean_bps) forces fade_ok False.
    osc = [cc(100 + (i % 2), 1.0) for i in range(48)]
    btc = [cc(200.0, 0.05) for _ in range(48)]
    td = {"5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0},
          "1h": {"buy_share_pct": 50.0}}
    book = {"execution": {"microprice_dev_bps": -9.0}}    # book rolling hard down
    out = regime.classify({"15m": osc, "1h": osc}, {"15m": btc}, td, book)
    assert out["regime_label"] == "ranging"
    assert out["fade_ok"] is False


def test_classify_degrades_gracefully_on_missing_data():
    out = regime.classify({"15m": [], "1h": []}, {"15m": []}, "DATA UNAVAILABLE", {})
    assert out["regime_label"] == "unknown"
    assert out["fade_ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: FAIL — `AttributeError: module 'regime' has no attribute 'classify'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to regime.py

# Initial CANDIDATE thresholds — confirmed / retuned by backtest_thresholds.py
# (Task 6). Do not treat as final until the plateau sweep passes.
DEFAULT_PARAMS = {
    "recent_n": 6,
    "baseline_n": 20,
    "corr_lookback": 32,
    "compression_quiet": 0.6,     # below => quiet
    "compression_trend": 1.5,     # above => expanding/trend
    "directionality_trend": 0.5,  # above => trending
    "directionality_chop": 0.3,   # below (with compression) => quiet/chop
    "btc_corr_high": 0.7,         # above (with low directionality) => correlated-chop
    "two_sided_min": 0.6,         # above => mean-reverting tape
    "book_lean_bps": 6.0,         # |microprice_dev_bps| beyond => about-to-break guard
}


def classify(candles, btc_candles, taker_delta, book, *, params=None):
    """Compose primitives into a regime read. Pure; never raises.

    candles/btc_candles: {interval: [candle,...]} dicts (as fetch_market builds).
    taker_delta: the bucketed dict (or a 'DATA UNAVAILABLE' string).
    book: the fetch_l2 dict (uses book['execution']['microprice_dev_bps']).
    Returns a dict safe to JSON-serialize and attach as out['regime'].
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    c15 = candles.get("15m", []) if isinstance(candles, dict) else []
    c1h = candles.get("1h", []) if isinstance(candles, dict) else []
    b15 = btc_candles.get("15m", []) if isinstance(btc_candles, dict) else []
    td = taker_delta if isinstance(taker_delta, dict) else {}

    comp = range_compression(c15, p["recent_n"], p["baseline_n"])
    direction = directionality(c1h)
    corr = btc_corr(c15, b15, p["corr_lookback"])
    tsf = two_sided_flow(td)
    dev = None
    if isinstance(book, dict):
        ex = book.get("execution") or {}
        dev = ex.get("microprice_dev_bps")

    label = _label(comp, direction, corr, tsf, p)
    book_safe = dev is not None and abs(float(dev)) <= p["book_lean_bps"]
    fade_ok = (label == "ranging") and book_safe

    return {
        "range_compression": comp,
        "directionality": direction,
        "btc_corr": corr,
        "two_sided_flow": tsf,
        "microprice_dev_bps": dev,
        "regime_label": label,
        "fade_ok": fade_ok,
        "params": p,
        "_note": ("regime_label in {trending,ranging,quiet,correlated-chop,unknown}. "
                  "fade_ok is COARSE in Phase 1 (regime + book-lean only); the "
                  "trade-grade edge-proximity guard arrives in passive mode (Plan 2). "
                  "Passive fades only when regime_label=ranging AND fade_ok=true; "
                  "quiet/correlated-chop => sit out; trending => directional only."),
    }


def _label(comp, direction, corr, tsf, p):
    if comp is None or direction is None:
        return "unknown"
    if comp < p["compression_quiet"] and direction < p["directionality_chop"]:
        return "quiet"
    if corr is not None and corr > p["btc_corr_high"] and direction < (p["directionality_trend"] - 0.1):
        return "correlated-chop"
    if direction > p["directionality_trend"] or comp > p["compression_trend"]:
        return "trending"
    if tsf is not None and tsf >= p["two_sided_min"]:
        return "ranging"
    return "quiet"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_regime.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add regime.py tests/test_regime.py
git commit -m "feat(regime): classify() composing label + coarse fade_ok"
```

---

## Task 4: Wire `regime` into `fetch_market.py` (+ BTC 15m candles)

**Files:**
- Modify: `fetch_market.py:546-549` (btc_candles fetch + attach regime)
- Test: `tests/test_fetch_market.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fetch_market.py
# (file already does: sys.path.insert(...); import fetch_market)

def test_assemble_attaches_regime_block(monkeypatch):
    import fetch_market as fm

    def fake_series(closes):
        return [{"t": "x", "o": x, "h": x + 0.5, "l": x - 0.5, "c": x, "v": 1.0}
                for x in closes]

    osc = fake_series([100, 101, 100, 101, 100, 101] * 8)

    monkeypatch.setattr(fm, "fetch_ctx", lambda coin, dex=None: {
        "coin": coin, "mark": 100.0, "oracle": 100.0, "mid": 100.0,
        "funding": 0.0, "premium": 0.0, "oi_usdc": 0.0,
        "prev_day_px": 100.0, "day_vol_usdc": 0.0})
    monkeypatch.setattr(fm, "fetch_candles",
                        lambda coin, interval, s, e: osc)
    monkeypatch.setattr(fm, "fetch_l2", lambda coin: {
        "asks": {}, "bids": {},
        "execution": {"microprice_dev_bps": 0.1, "microprice": 100.0}})
    monkeypatch.setattr(fm, "fetch_recent_trades", lambda coin: [])
    monkeypatch.setattr(fm, "merge_trade_cache", lambda c, f, n: [])
    monkeypatch.setattr(fm, "bucket_taker_delta", lambda t, n: {
        "5m": {"buy_share_pct": 50.0}, "15m": {"buy_share_pct": 50.0},
        "1h": {"buy_share_pct": 50.0}})
    monkeypatch.setattr(fm, "fetch_btc_dominance", lambda: {"btc_d": 55.0})
    monkeypatch.setattr(fm, "update_btcd_cache", lambda d, n: [])
    monkeypatch.setattr(fm, "compute_btcd_24h_change", lambda r, n, d: {
        "btc_d_24h_chg": None, "btc_d_sample_age_min": None, "btc_d_coverage_h": 0.0})

    out = fm.assemble("HYPE", now_ms=1_750_000_000_000)
    assert "regime" in out
    assert out["regime"]["regime_label"] in {
        "trending", "ranging", "quiet", "correlated-chop", "unknown"}
    # BTC 15m candles must be fetched for correlation
    assert "15m" in out["btc_candles"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_fetch_market.py::test_assemble_attaches_regime_block -v`
Expected: FAIL — `KeyError: 'regime'` (and/or `'15m'` missing from btc_candles)

- [ ] **Step 3: Implement — add import, BTC 15m candles, attach regime**

At the top of `fetch_market.py` with the other imports (after line 13's `from datetime import timedelta`), add:

```python
import regime as regime_mod
```

Replace the BTC candles fetch (currently `fetch_market.py:546-547`):

```python
    out["btc_candles"] = {iv: fetch_candles("BTC", iv, now_ms - span, now_ms)
                          for iv, span in (("4h", 12 * _DAY), ("1h", 3 * _DAY))}
```

with (adds 15m, needed for correlation against the primary 15m series):

```python
    out["btc_candles"] = {iv: fetch_candles("BTC", iv, now_ms - span, now_ms)
                          for iv, span in (("4h", 12 * _DAY), ("1h", 3 * _DAY),
                                           ("15m", 9000000))}
```

Immediately after the `out["ath_state"] = ...` line (`fetch_market.py:549`), add:

```python
    out["regime"] = regime_mod.classify(
        out["candles"], out["btc_candles"],
        out.get("taker_delta"), out.get("book"))
```

Note ordering: `out["taker_delta"]` and `out["book"]` must already be set. `book` is set at line 548 (before ath_state) and taker_delta is set in the try-block at 551-562 — so move the `out["regime"] = ...` assignment to AFTER the taker_delta try/except block (after line 562), not right after ath_state. Place it just before the BTC dominance block at line 564.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_fetch_market.py::test_assemble_attaches_regime_block -v`
Expected: PASS

- [ ] **Step 5: Run the full suite + a live smoke check**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest -q`
Expected: all pass.

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 fetch_market.py HYPE | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['regime'], indent=2))"`
Expected: a populated `regime` block with a real `regime_label` and `fade_ok`.

- [ ] **Step 6: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add fetch_market.py tests/test_fetch_market.py
git commit -m "feat(fetch): attach regime block; fetch BTC 15m for correlation"
```

---

## Task 5: Read-only "market weather" line in `scalp-core.md`

**Files:**
- Modify: `scalp-core.md` (Step 1 data section + QUICK/TINY output templates)
- Test: `tests/test_skill_consistency.py`

- [ ] **Step 1: Write the failing test**

`tests/test_skill_consistency.py` greps the skill markdown for required strings. Add a check that the weather line is documented.

```python
# append to tests/test_skill_consistency.py

def test_core_documents_regime_weather_line():
    import os
    core = os.path.join(os.path.dirname(__file__), "..", "scalp-core.md")
    with open(core) as f:
        text = f.read()
    # the regime read must be wired into the output, and must be read-only this phase
    assert "regime" in text.lower()
    assert "WEATHER" in text
    assert "fade_ok" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_skill_consistency.py::test_core_documents_regime_weather_line -v`
Expected: FAIL — assertion error (`WEATHER` not found).

- [ ] **Step 3: Add the weather line to `scalp-core.md`**

In `scalp-core.md`, in the Step 1 data section (after the paragraph describing `book.execution`, near the `session.weekend_window` paragraph), add:

```markdown
The output includes `regime` — a deterministic market-regime read (range
compression, BTC correlation, two-sided-flow score, directionality) with a
`regime_label` ∈ {trending, ranging, quiet, correlated-chop, unknown} and a
coarse `fade_ok` boolean. **Phase 1: READ-ONLY.** Surface it as the WEATHER
line below; it does NOT change directional verdicts yet. Its purpose is the
@BeerusS0L insight — directional edge is poor in `quiet` / `correlated-chop`
tape. When `regime_label` is `quiet` or `correlated-chop`, add one sentence to
the output noting directional edge is structurally low here.
```

In BOTH the QUICK action and QUICK no-action output templates, add a line directly under the `VERDICT:` line:

```markdown
WEATHER: <regime_label> (compression <x> | BTC-corr <x> | 2-sided <x> | fade_ok <bool>)
```

In the TINY ENTRY template, append ` | wx <regime_label>` to the single line.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_skill_consistency.py::test_core_documents_regime_weather_line -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add scalp-core.md tests/test_skill_consistency.py
git commit -m "feat(skill): read-only WEATHER line surfacing the regime read"
```

---

## Task 6: Backtest-validate regime thresholds (the §7 gate)

**Files:**
- Modify: `backtest_thresholds.py` (add regime-validation routine + CLI subcommand)
- Test: `tests/test_backtest.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_backtest.py


def test_regime_forward_outcomes_separates_ranging_from_trending():
    # Synthetic: a long ranging block (oscillation) then a clean trend block.
    # BTC is a SEPARATE flat series so btc_corr is undefined (None) -> the
    # correlated-chop rule is out of the way, and the structural gate sorts
    # ranging vs trending. The validator must report mean |forward return|
    # LOWER after 'ranging' bars than after 'trending' bars.
    import backtest_thresholds as bt
    ranging = []
    for i in range(60):
        px = 100.0 + (1.0 if i % 2 else -1.0)  # oscillate 99/101
        ranging.append(c(T0 + i * H1, px, px + 0.5, px - 0.5, px))
    trend = []
    for i in range(60):
        p = 101.0 + i  # clean up
        trend.append(c(T0 + (60 + i) * H1, p, p + 0.5, p - 0.5, p))
    series = ranging + trend
    btc_flat = [c(T0 + i * H1, 200.0, 200.05, 199.95, 200.0) for i in range(120)]
    stats = bt.regime_forward_stats(series, btc_flat, horizon_bars=4)
    assert stats["ranging"]["n"] > 0
    assert stats["trending"]["n"] > 0
    # trending bars precede larger forward net moves than ranging bars
    assert stats["trending"]["mean_abs_fwd_ret"] > stats["ranging"]["mean_abs_fwd_ret"]
```

(Reuse the `c(t, o, h, l, cl)` helper and `T0`/`H1` constants already at the top of `tests/test_backtest.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_backtest.py::test_regime_forward_outcomes_separates_ranging_from_trending -v`
Expected: FAIL — `AttributeError: module 'backtest_thresholds' has no attribute 'regime_forward_stats'`

- [ ] **Step 3: Implement the validation routine**

Add to `backtest_thresholds.py`:

```python
import regime as _regime_mod

# Flow (taker_delta) has no multi-day history (the live trade cache spans ~6h),
# so the backtest cannot reconstruct the two-sided-flow signal. It assumes
# NEUTRAL two-sided flow and validates only the STRUCTURAL regime gate
# (range_compression + directionality + btc_corr). The live `ranging` label
# additionally requires real two-sided flow at decision time — that half is not
# backtestable from candles and is validated live via the audit log instead.
_NEUTRAL_FLOW = {"5m": {"buy_share_pct": 50.0},
                 "15m": {"buy_share_pct": 50.0},
                 "1h": {"buy_share_pct": 50.0}}
_BACKTEST_WINDOW = 48  # trailing bars handed to classify (mimics live bounded candle feed)


def regime_forward_stats(primary_candles, btc_candles, horizon_bars=4,
                         params=None):
    """At each bar i (given enough history), label the regime from a TRAILING
    window of candles ending at i, then measure the forward net return over the
    next `horizon_bars`. Group by label. A useful classifier shows: 'trending'
    bars precede LARGE net moves (continuation) and 'ranging' bars precede SMALL
    net moves (mean-reversion). Returns {label: {n, mean_abs_fwd_ret, mean_fwd_ret}}.

    A trailing window (not the full prefix) is used so directionality reflects
    recent structure, mirroring the live bounded candle feed. Flow is assumed
    neutral (see _NEUTRAL_FLOW) -> validates the structural gate only.
    """
    p = {**_regime_mod.DEFAULT_PARAMS, **(params or {})}
    need = max(p["baseline_n"], p["corr_lookback"])
    buckets = {}
    n = len(primary_candles)
    for i in range(need, n - horizon_bars):
        lo = max(0, i + 1 - _BACKTEST_WINDOW)
        seg = primary_candles[lo:i + 1]
        bseg = btc_candles[lo:i + 1]
        out = _regime_mod.classify({"15m": seg, "1h": seg}, {"15m": bseg},
                                   _NEUTRAL_FLOW, {"execution": {}}, params=p)
        label = out["regime_label"]
        c0 = float(primary_candles[i]["c"])
        c1 = float(primary_candles[i + horizon_bars]["c"])
        if c0 <= 0:
            continue
        fwd = (c1 - c0) / c0
        b = buckets.setdefault(label, {"n": 0, "_sum_abs": 0.0, "_sum": 0.0})
        b["n"] += 1
        b["_sum_abs"] += abs(fwd)
        b["_sum"] += fwd
    result = {}
    for label, b in buckets.items():
        result[label] = {
            "n": b["n"],
            "mean_abs_fwd_ret": round(b["_sum_abs"] / b["n"], 6),
            "mean_fwd_ret": round(b["_sum"] / b["n"], 6),
        }
    return result


def regime_compression_sweep(primary_candles, btc_candles, horizon_bars=4,
                             grid=(0.4, 0.5, 0.6, 0.7, 0.8)):
    """Sweep compression_quiet to check the chosen value sits on a plateau, not
    a spike. Returns {value: {ranging_abs, trending_abs, separation}} so a human
    can confirm stable behavior around DEFAULT_PARAMS."""
    rows = {}
    for v in grid:
        stats = regime_forward_stats(primary_candles, btc_candles,
                                     horizon_bars=horizon_bars,
                                     params={"compression_quiet": v})
        rang = stats.get("ranging", {}).get("mean_abs_fwd_ret")
        trend = stats.get("trending", {}).get("mean_abs_fwd_ret")
        sep = (trend - rang) if (rang is not None and trend is not None) else None
        rows[v] = {"ranging_abs": rang, "trending_abs": trend, "separation": sep}
    return rows
```

Add a CLI subcommand so it's runnable against live history. In the `main()` of `backtest_thresholds.py`, add a `--regime` branch that fetches candles via `fetch_market` and prints `regime_forward_stats` + `regime_compression_sweep` as JSON. (Follow the existing arg-parsing pattern in the file; fetch the primary and BTC 1h candles over `--days`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 -m pytest tests/test_backtest.py::test_regime_forward_outcomes_separates_ranging_from_trending -v`
Expected: PASS

- [ ] **Step 5: Run the live validation (THE GATE)**

Run: `cd /Users/nyanyk/Claude/research/scalp && python3 backtest_thresholds.py --regime --coin HYPE --days 180`

Inspect the output and record it in `CHANGELOG.md`. **Gate criteria (spec §7):**
- `ranging` bars must precede materially SMALLER mean-abs-forward-returns than `trending` bars (the classifier actually separates mean-reverting from trending tape).
- The compression sweep must show the chosen `compression_quiet` (0.6) sitting on a stable plateau, not a lone spike.

**If the gate FAILS** (ranging and trending are indistinguishable, or no plateau): STOP. Do not proceed to Plan 2. Retune `DEFAULT_PARAMS`, re-run, or escalate that the regime signal has no edge on this instrument — per spec §6, the passive mode does not ship without a working classifier.

- [ ] **Step 6: Commit**

```bash
cd /Users/nyanyk/Claude/research/scalp
git add backtest_thresholds.py tests/test_backtest.py CHANGELOG.md
git commit -m "feat(backtest): regime forward-outcome validation + compression sweep"
```

---

## Done criteria for Plan 1

- [ ] `python3 -m pytest -q` green (regime unit tests, fetch wiring, skill-consistency, backtest).
- [ ] `python3 fetch_market.py HYPE` emits a populated `regime` block.
- [ ] `/scalp HYPE` shows the WEATHER line (read-only; verdicts unchanged).
- [ ] Backtest gate (Task 6 Step 5) recorded in `CHANGELOG.md` with a pass/fail decision.

## After Plan 1 — what Plan 2 covers (NOT in scope here)

Authored only after the Task 6 gate passes, using the validated thresholds:
- `scalp-passive.md` module (both-sides laddered passive entries, to-the-mean exit, three-way invalidation).
- Trade-grade edge-proximity `fade_ok` guard (needs the LLM-computed range floor/ceiling, hence Plan 2).
- Expectancy-based sizing + `UNPROVEN/PROVEN` gate (`profile.py`, sizing math).
- `audit_log.py` `setup_family` field + summary breakout.
- `behavioral.py` passive carve-out + tilt guard.
- `replay.py` passive exit simulation.
- `/scalp passive <COIN>` invocation wiring in `SKILL.md` + `scalp-core.md`.
