# Scalp — SHORT module

Loaded with `scalp-core.md` when the user invokes `/scalp short <COIN>`.
Provides Steps 2–4 and the short output deltas. Steps 0,1,5,6,M and the
output/journal skeleton come from `scalp-core.md`. Short setups only.

## Step 2 — Macro veto gate (short; runs after behavioral preflight)
VETO SHORTS if any:
- BTC broke a defined structural level (prior swing high / range ceiling) to
  the UPSIDE on rising volume within the trade horizon; OR
- btc_d_24h_chg <= -1.5 and BTC.D falling (alts ripping, dominance bleeding
  — anti-short for alts); OR
- BTC 1h range >= 2x its trailing 20-bar average range, directional UP; OR
- **Binary US econ event today**: FOMC decision, FOMC minutes, CPI, NFP,
  PCE — NO-TRADE for the day regardless of setup quality (symmetric with the
  long gate; event-day uncertainty kills both directions). Ask user once if
  the calendar is unclear; default to VETO if uncertain on a known event
  date; OR
- **Funding extreme negative**: this perp's funding < -0.05% per 8h
  (annualized < -55%) — crowded short, NO new short entries; MANAGE only.
If `macro_can_clear` is false (BTC.D unavailable) -> veto CANNOT clear ->
verdict NO-TRADE on any short bias.
Otherwise macro is a size modifier:
- BTC.D rising + BTC soft -> tailwind for shorts, full size
- BTC.D falling + BTC bid -> headwind, half size, tighter targets
- chop -> neutral
Always print: `MACRO: CLEAR` or `MACRO: VETO (reason)`. Note trajectory if
you can infer it from BTC.D 24h chg sign + recent movement.

Funding meaning is INVERTED for shorts: positive funding is a TAILWIND (you
are paid to hold, and crowded longs are fuel for a flush). Only extreme
NEGATIVE funding vetoes.

### Weekend modifier (size, not veto)
If `session.weekend_window` is true (Fri 20:00 – Sun 20:00 UTC): size ×0.5,
targets tighter, faster time-stop. Thin weekend books cut both ways. Apply
this on top of any macro size modifier. Print the line: `WEEKEND: size x0.5`.

## Step 3 — Structure engine (short; primary coin)
From candles: ceiling (>=2 tested highs; flag sweep+rejection), floor (>=2
tested lows = breakdown target), mid pivot. Classify: range / breakdown /
failed-breakout / downtrend.
Positioning from funding+premium+OI+taker_delta (inverted read):
- Funding positive + premium positive + buy_share extreme high = crowded
  long = SHORT FUEL
- Funding negative + premium negative + sell_share extreme = crowded short =
  AVOID (no fuel, squeeze risk)

**ATH / discovery state** (read `ath_state` from fetch — it's already computed):
- `below_ath` — standard structure analysis; ceiling = recent prior resistance.
- `approaching_ath` / `at_ath_zone` — PRIME short zone, but ONLY if the first
  ATH touch has already rejected. A failed retest of ATH is the cleanest
  short. If ATH has not yet been touched, wait — do not front-run the level.
- `above_ath_discovery` (>0.5% above ATH) — DO NOT SHORT. Fighting price
  discovery with no overhead supply. Force NO-TRADE regardless of trigger
  quality.

## Step 4 — Triggers (short only)
- A Failed-breakout (sweep-rejection): price sweeps above a tested ceiling
  (runs short stops) -> fails to hold -> 5m closes back below the ceiling ->
  entry on the close or on a retest of the ceiling from below; stop above the
  sweep overshoot wick. Targets: T1 mid pivot, T2 floor.
- B Breakdown (momentum-break down): 5m/15m close BELOW a tested floor on
  expanding volume -> entry on the close or on a retest of the broken floor
  from below; stop above the broken floor (now resistance) + structural
  buffer. Targets: next structural level down, or measured-move down.

  Confidence modifier from `taker_delta` in the breakdown window (NOT a gate —
  do not skip an otherwise valid breakdown just because this is weak):
  - sell_share_pct > 60% + coverage_pct sufficient → HIGH-conviction
    breakdown, full size acceptable
  - sell_share_pct 40–60% → standard; size normally
  - sell_share_pct < 40% → WEAK breakdown (long-squeeze bounce risk);
    cut size or wait for the retest before entering
  (sell_share_pct = 100 − buy_share_pct from taker_delta.)
- C Lower-high rejection: after a top, price rallies to a lower high vs the
  prior swing high and rejects (wick + sell-dominant taker delta) -> entry on
  rejection confirmation (5m close back down); stop above the lower high.
  Targets: prior swing low / floor.
Each: entry / stop / targets / R:R from live levels.

**Sweep stop rule (short)**: identify the next structural pool ABOVE entry
(prior swing high, broken support, round-number magnet). Stop goes above
THAT, not above the entry-trigger wick. If the stop distance breaks the risk
budget at full size, CUT SIZE — never tighten into noise.

## Output deltas (short)
The ENTRY QUICK / DEEP and MANAGE skeletons come from `scalp-core.md`. Fill:
- VERDICT vocabulary: `SHORT-NOW | WAIT | NO-TRADE | VETOED | HALT`
- The counter-case line is **"Bull case"** — one clause on what kills the
  short in the next 1h (an upside surprise).
- Triggers labelled A (failed-breakout) / B (breakdown) / C (lower-high).
- Journal stub `side:` field = `short`.
- Print the `WEEKEND: size x0.5` line whenever `session.weekend_window` is true.
