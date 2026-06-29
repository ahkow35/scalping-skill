# Scalp — LONG module

Loaded with `scalp-core.md` when the user invokes `/scalp <COIN>` (default).
Provides Steps 2–4 and the long output deltas. Steps 0,1,5,6,M and the
output/journal skeleton come from `scalp-core.md`. Long setups only.

## Step 2 — Macro veto gate (long; runs after behavioral preflight)
VETO LONGS if any:
- BTC broke a defined structural level (prior swing low / range floor) to the
  downside on rising volume within the trade horizon; OR
- btc_d_24h_chg >= +1.5 and BTC.D rising; OR
- **Binary US econ event today**: FOMC decision, FOMC minutes, CPI, NFP,
  PCE — NO-TRADE for the day regardless of setup quality. Ask user once
  if the calendar is unclear; default to VETO if uncertain on a known
  event date; OR
- **Funding extreme positive**: this perp's funding > +0.03% per 8h
  (annualized >33%) — crowded long, NO new long entries; MANAGE only.
  (Tightened from 0.05 on 2026-06-10: backtest showed clear negative
  forward drift already at 0.03%/8h — 24h mean −1.85% vs +0.38% baseline.)
If `macro_can_clear` is false (BTC.D snapshot unavailable) -> veto CANNOT
clear -> verdict NO-TRADE on any long bias.

**BTC.D 24h-change cache warm-up**: `btc_d_24h_chg` is derived from a local
rolling snapshot cache (CoinGecko's `/global` does not expose historical
BTC.D change). When `btc_d_24h_chg` is `null`, the cache has not yet
accumulated a sample near 24h ago (target ±90min) — SKIP only the
BTC.D-component of the veto; other macro checks (BTC structural break,
funding extremes, event days) still apply normally. Note `btc_d_coverage_h`
in the verdict output when this happens so the user knows the macro gate
is partial.
Otherwise macro is a size modifier:
- BTC.D falling + BTC bid -> tailwind, full size
- BTC.D rising + BTC up -> headwind, half size, tighter targets
- **BTC volatility spike** (BTC 1h range >= 2x trailing 20-bar avg, either
  direction): half size + widen the structural stop (cut size, never
  tighten) + tighter time-box. NOT a veto — demoted 2026-06-10: backtest
  showed down-spikes preceded BETTER 24h returns (capitulation bounce,
  +1.13% vs +0.29%) but WORSE 4h adverse excursion (−1.84% vs −1.54%).
  The spike marks stop-out risk, not continuation.
- chop -> neutral
Always print: `MACRO: CLEAR` or `MACRO: VETO (reason)`. Note trajectory if
you can infer it from BTC.D 24h chg sign + recent movement.

## Step 3 — Structure engine (long; primary coin)
From candles: floor (>=2 tested lows; flag sweep+reclaim), ceiling (>=2 tested
highs), mid pivot. Classify: range / breakout / breakdown / trend.
Positioning from funding+premium+OI+taker_delta:
- Funding ~0 or negative + premium negative + low buy_share = NOT crowded
- Funding rising positive + premium positive + buy_share extreme = crowded long

**ATH / discovery state** (read `ath_state` from fetch — it's already computed):
- `below_ath` — standard structure analysis; ceiling = recent prior resistance.
- `approaching_ath` (within 5% of ATH) — ATH is the dominant magnet AND the
  primary supply zone. Triggers and targets pivot around it; expect supply
  to lift offers as price approaches.
- `at_ath_zone` (within 0.5% of ATH) — first touch usually rejects. Do NOT
  treat this as a breakout. Wait for 5m/15m close ABOVE ath_price + a retest
  that holds before flipping to discovery framing.
- `above_ath_discovery` (>0.5% above ATH) — NO overhead supply exists.
  Target logic switches: use measured-move (recent range × 1.0–1.618) +
  round-number magnets ($X0, $X5). Do NOT cite prior-resistance targets
  because there aren't any. Stop = prior ATH as new support, plus a
  structural buffer.

## Step 4 — Triggers (long only)
- A Sweep-reclaim: stop-run below tested floor -> 5m reclaim -> entry; stop
  below the **next structural pool** (not below noise wick).
- B Momentum-break: 5m/15m close above ceiling on expanding volume -> entry;
  target next structural level (or discovery measured-move if ath_state =
  above_ath_discovery).

  Confidence modifier from `taker_delta` in the break window (NOT a gate —
  do not skip an otherwise valid break just because this is weak):
  - buy_share_pct > 60% + coverage_pct sufficient → HIGH-conviction break,
    full size acceptable
  - buy_share_pct 40–60% → standard; size normally
  - buy_share_pct < 40% → WEAK break (short-covering, not real buying);
    cut size or wait for the retest before entering
Each: entry / stop / targets / R:R from live levels.

**Sweep stop rule**: identify the next structural pool below entry (prior
swing low, broken resistance, round-number magnet). Stop goes below THAT,
not below the entry-trigger wick. If the stop distance breaks the risk
budget at full size, CUT SIZE — never tighten into noise.

## Output deltas (long)
The ENTRY QUICK / DEEP and MANAGE skeletons come from `scalp-core.md`. Fill:
- VERDICT vocabulary:
  `LONG-NOW | LONG-CLOSE | LONG-PROBE | WAIT | NO-TRADE | VETOED | HALT`
  - `LONG-NOW` (high conviction → 100% of cap)
  - `LONG-CLOSE` (med conviction → 50% of cap)
  - `LONG-PROBE` (low conviction → 25% of cap)
- The counter-case line is **"Bear case"** — one clause on what kills the long
  in the next 1h.
- Triggers labelled A (sweep-reclaim) / B (momentum-break). No trigger C.
- Journal stub `side:` field = `long`.
- The `WEEKEND:` line is omitted for longs (weekend modifier is short-only).
