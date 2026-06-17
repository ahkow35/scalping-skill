# Scalp — PASSIVE module

Loaded with `scalp-core.md` when the user invokes `/scalp passive <COIN>`.
Provides Steps 2–4 and the passive output deltas. MM-INSPIRED, NOT market
making: a manually-executed, both-sides mean-reversion fade that posts passive
limit ladders at range edges and exits to the mean. Steps 0,1,5,6,M and the
output/journal skeleton come from `scalp-core.md`.

This mode fades BOTH edges of a range; only ONE side arms at a time — whichever
edge price is currently AT.

## Step 2 — Gates (passive; ALL must pass or SIT-OUT / NO-TRADE)

1. **Regime gate (hard):** require `regime.fade_ok == true` AND
   `regime.regime_label == "ranging"`. Otherwise → **SIT-OUT**: print the label
   and the reason — `trending` (directional only; use /scalp long|short),
   `quiet` (no edge either way), `correlated-chop` (just BTC beta — no
   idiosyncratic edge), `unknown` (insufficient data). Passive fading only has
   an edge in genuine two-sided ranging tape.
2. **Expectancy gate (hard):** read `python3 audit_log.py passive-expectancy`.
   If `state == "STOP"` → **NO-TRADE** (the audit log says this mode is
   net-negative over ≥40 trades — stop firing it). Otherwise carry `size_mult`
   into sizing (UNPROVEN → 0.25, PROVEN → 1.0).
3. **Event veto (hard):** binary US econ event today (FOMC/CPI/NFP/PCE) →
   **VETOED / NO-TRADE**, same as the directional modules. (Funding extremes do
   NOT veto a both-sides fade — note crowded funding as context only.)
4. **Tilt (soft):** if `behavioral.passive_tilt.active` (≥3 consecutive passive
   losses) → print `PASSIVE TILT: <n> losses — range likely breaking` and prefer
   SIT-OUT; if proceeding, halve size. Informational, not a hard stop.

## Step 3 — Structure (passive)

From candles: **floor** (≥2 tested lows), **ceiling** (≥2 tested highs),
**mean** (session VWAP; fallback range-mid = (floor+ceiling)/2). The `ranging`
regime label already implies a real range, not a coiling breakout. Define the
**edge zones** = the outer third of the range nearest each boundary. Price
between the inner thirds is mid-range → **WAIT** (no fade arms mid-range).

## Step 4 — Triggers (passive; both-sides, one arms at a time)

- **A Bid-ladder (long fade)** — arms only when ALL hold:
  - price is in the LOWER edge zone (≤ floor + ⅓·range), AND
  - flow into the low is EXHAUSTING (taker_delta sell pressure waning, or
    `buy_share_pct` recovering off the low), AND
  - microprice is NOT leaning hard down through the floor (Step 6d:
    `microprice_dev_bps` not strongly negative).
  Post 2–3 passive limit bids stepped INTO the floor: at floor, floor−X%,
  floor−2X% (X ≈ 0.2–0.4% sized to the range). Never market-take, never chase.
- **B Ask-ladder (short fade)** — symmetric at the ceiling (price in upper edge
  zone; sell-flow exhausting; microprice not leaning hard up).

**Edge-proximity rule:** if price is mid-range (between the inner thirds) →
WAIT, do not arm either ladder. This is the trade-grade `fade_ok` — coarse
regime `fade_ok` PLUS at-an-edge PLUS flow-exhaustion.

Each armed ladder states: entries (the steps) / structural stop (beyond the far
step + buffer; for the bid-ladder below the next pool under the floor) / mean
target / R per step.

## Step 5/6 — Sizing & risk (passive)

- **Sizing:** `risk = equity × cap × size_mult` (cap from `profile.py`;
  `size_mult` from the Step-2 expectancy gate). The WHOLE ladder counts as ONE
  position against `risk` — sum of step sizes ≤ risk (inventory cap; laddering
  never multiplies risk). Show the sizing line per ladder.
- **Session shot-cap:** max 3 passive scalps per session — state the count in
  output; at the cap → WAIT.
- **R:R note:** the directional 2:1 floor does NOT apply — passive is a
  high-win-rate/low-R mode gated by expectancy, not per-trade R:R. (See
  `passive_expectancy`.)

## Exit (passive) — to-the-mean

Target = the mean (session VWAP / range-mid). On fill, post a limit at the mean.
Default = full exit at the mean (small R, high turnover — the MM-inspired core).
Optional: leave a small runner a touch beyond the mean only if flow strongly
supports; default is flat at mean.

## Invalidation (passive) — three exits, FASTEST wins

1. **Hard structural stop** beyond the far ladder step (submitted to HL) — bounds
   the left tail (the range-break loss).
2. **Quick-pull (the MM analog):** if after fill the taker flow turns MORE
   one-sided AGAINST the position (range breaking), exit immediately — do not
   wait for the stop.
3. **Time-stop:** no reversion toward the mean within ~30–60 min → flat.

## Output deltas (passive)

- VERDICT vocab: `FADE-LONG-NOW | FADE-SHORT-NOW | WAIT | SIT-OUT | NO-TRADE | VETOED`.
- Counter-case line is **"Break case"** — one clause on what says the range is
  breaking (which kills the fade).
- Triggers labelled **A (bid-ladder)** / **B (ask-ladder)**.
- Show `EXPECTANCY: <state> (n <n>, win <w>%, avg <avg>R, size ×<mult>)` on the
  verdict line.
- Journal stub `side:` = `long` (bid-ladder) or `short` (ask-ladder);
  `setup_family: passive-fade`.
- TINY: `<COIN> PASSIVE <V> | <expectancy_state> | edge <floor|ceil|mid> @ <mid> | fade_ok <bool> | next <when>`.

## Audit logging (passive)

Log every FADE-*/WAIT/SIT-OUT/NO-TRADE/VETOED with `mode: "ENTRY"`,
`setup_family: "passive-fade"`, and the verdict. JOURNAL STUB shown only for
FADE-LONG-NOW / FADE-SHORT-NOW. On close, resolve with the R-multiple so
`passive_expectancy` learns (UNPROVEN → PROVEN/STOP).
