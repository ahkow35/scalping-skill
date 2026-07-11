"""RVOL (relative volume) — the admission-layer volume signal for the
volume-filter backtest (see PLAN-volume-backtest-2026-07-11.md).

Definition (frozen in the plan): signal-bar volume / mean of the PRIOR 20 bars
on the SAME timeframe, excluding the signal bar itself from the mean. Guards
divide-by-zero (no/zero volume in the window) by returning None — the caller
treats an undefined RVOL as "excluded from binned analysis, never passes a
filter" (never silently coerced to 0 or skipped-as-pass).

Pure; no I/O. Shared by backtest.py (admission layer) and analysis_volume.py
(A1 quartile diagnostic).
"""

RVOL_WINDOW = 20


def rvol(candles, i, window=RVOL_WINDOW):
    """RVOL of candles[i]: its volume divided by the mean volume of the prior
    `window` bars (candles[i-window:i], signal bar excluded). Returns None if:
      - i is out of range or there are no prior bars at all (i <= 0), or
      - the prior window's mean volume is zero/absent (divide-by-zero guard).
    Fewer than `window` prior bars available (early in a series) still
    computes over whatever prior bars exist — a short-window RVOL, not a
    missing one; only a genuinely empty or all-zero window is None.
    """
    if candles is None or i is None or i <= 0 or i >= len(candles):
        return None
    prior = candles[max(0, i - window):i]
    if not prior:
        return None
    vols = [float(c.get("v", 0.0) or 0.0) for c in prior]
    mean_v = sum(vols) / len(vols)
    if mean_v <= 0:
        return None
    sig_v = float(candles[i].get("v", 0.0) or 0.0)
    return sig_v / mean_v
