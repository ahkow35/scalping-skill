# Spec — Passive Flow-Capture Mode (`/scalp passive`)

- **Date:** 2026-06-17
- **Status:** Design approved (Chunks A–C), pending spec review → implementation plan
- **Author:** Coding Claude + Nyan (brainstorming session)
- **Repo:** `/Users/nyanyk/Claude/research/scalp`

---

## 1. Motivation

@BeerusS0L (quant, order-flow / algo market making) makes two claims:

1. **Order flow is the core alpha.**
2. **Directional trading suffers in quiet, correlated markets** — so he monetizes
   flow directly (market making) instead of betting direction.

The scalp skill **already embodies claim 1**: it has real taker-aggressor delta
(`taker_delta`), a Stoikov microprice execution gate (Step 6d — literally the
"don't get picked off paying the wrong side of fair value" mitigation), macro
veto, behavioral preflight, and an audit log.

The skill **does not embody claim 2**: it has no explicit *regime awareness*.
Nothing detects "this tape is too quiet / too BTC-correlated to have directional
edge — sit out." This gap is exactly what produced four consecutive WAITs in
chop on 2026-06-16: the skill *accidentally* waited; it never *diagnosed why*.

This spec adds a **passive flow-capture mode** that captures the *spirit* of
market making for a retail, manually-executed trader, plus the **regime
classifier** that both gates it and delivers the missing defensive half.

## 2. Non-goals (explicit)

- **NOT literal market making.** The skill produces theses on a manual / 5-minute
  `/loop` cadence. Real MM is sub-second two-sided quote management; on that
  cadence the user is structurally the slowest quoter and would be adversely
  selected on every resting quote. Confirmed infeasible given **manual
  execution** (user reads output and clicks). This mode is MM-*inspired*, not MM.
- **NOT auto-routing (yet).** The existing `/scalp <COIN>` and `/scalp short
  <COIN>` directional flows are **untouched**. Passive mode is **opt-in** via a
  new invocation. Auto-routing (Approach 2) is a possible *future* promotion once
  the audit data proves the mode's edge — out of scope here.

## 3. Scope summary

A new direction-neutral protocol module `scalp-passive.md` beside
`scalp-long.md` / `scalp-short.md`, gated by a new deterministic `regime` block
in `fetch_market.py`. The mode fades *both* range edges with stepped passive
limit orders, exits fast to the mean, and is risk-gated by **empirical
expectancy from the audit log** rather than the directional 2:1 R:R floor.

---

## 4. Component design

### 4.1 Regime classifier (`fetch_market.py` → new `regime` block)

Deterministic, computed from data already fetched (primary + BTC candles,
`taker_delta`, `book`/microprice). Same pattern as `taker_delta`/`book`.

Outputs:

| Field | Definition | Source |
|---|---|---|
| `range_compression` | recent N-bar avg true range ÷ trailing-baseline avg range. <1 quiet, >1.5 expanding | primary candles |
| `btc_corr` | rolling correlation of primary vs BTC 15m returns over ~24–48 bars | primary + BTC candles |
| `two_sided_flow` | are aggressors oscillating near 50/50 (mean-reverting) vs one-sided (trending)? e.g. mean \|buy_share−50\| or flip-count over recent windows | `taker_delta` |
| `structure_state` | range / breakout / trend (already classified in Step 3) | reuse |
| `regime_label` | derived: **trending / ranging / quiet / correlated-chop** | composite |
| `fade_ok` | master boolean, **defaults FALSE** | composite + guards |

`regime_label` derivation:
- **trending** — expanding range + one-sided flow + structure ∈ {trend, breakout}.
  → momentum plays valid; passive fades REFUSED.
- **ranging** — bounded range, two-sided flow, moderate vol, tested edges holding.
  → passive fades FAVORED.
- **quiet** — `range_compression` very low, tiny ranges. → sit out (no edge either way).
- **correlated-chop** — `btc_corr` high AND BTC itself chopping. → sit out / downgrade
  (directional edge absent; fade risky because BTC can drag the range).

`fade_ok` is the **safety system** (conservative — prefers missing a fade to
catching a falling knife). It is TRUE only when:
- `regime_label == ranging`, AND
- price is NOT pinned at a range edge with one-sided flow building *and* the
  microprice leaning hard *through* that edge (the "about-to-break / picked-off"
  guard).

The classifier MUST be backtest-validated before its thresholds are trusted
(see §7).

### 4.2 Passive-fade protocol (`scalp-passive.md`)

Direction-agnostic at invocation. Reads the range and offers BOTH:
- a passive **bid ladder** at the floor (long fade — buy the flush), and
- a passive **ask ladder** at the ceiling (short fade — sell the rip).

**Entry — stepped passive limits ("stepped inventory"):**
- Ladder of 2–3 limit steps *into* the extreme (e.g. floor, floor−0.3%, floor−0.6%).
- **Microprice gate (reuse Step 6d):** a step arms only if the book is NOT leaning
  hard *through* the edge. Book rolling through the floor → ladder disarmed.
- **Flow confirmation:** arm only when the move into the edge is exhausting —
  sweep on declining aggressor volume, or buy_share recovering at the low. Fade
  absorption, never momentum.
- **Never** market-take, never chase.

**Exit — fast, to the mean:**
- Target = range mean (microprice / range-mid / session VWAP). Post limit there.
- Small R by design (~+0.3 to +0.8R). No structural T1/T2 ride.

**Invalidation — three exits, fastest wins:**
1. **Hard structural stop** below the lowest ladder step, submitted to HL — bounds
   the left tail.
2. **Quick-pull (MM analog):** flow turns *more* one-sided against the position
   after fill (range breaking) → exit immediately, do not wait for the stop.
3. **Time-stop:** no reversion within N bars (~30–60 min) → flat.

### 4.3 Risk math — expectancy gate (replaces the 2:1 floor for this mode)

A to-the-mean scalp is sub-1R per trade (e.g. +0.5R target / −1.5R stop = 0.33:1).
The directional 2:1 floor (Step 6c) would reject every passive trade, so passive
mode uses an **expectancy gate** instead:

- Breakeven win rate is computed and shown explicitly (e.g. +0.5/−1.5 → ~75% WR
  to break even). Fade scalping requires 70–80%+ or it bleeds.
- **`UNPROVEN` state:** until the audit log holds enough *resolved* passive-fade
  trades to show real positive expectancy (WR × avg-R > 0), the mode fires at
  **minimum size** and is flagged `UNPROVEN — collecting data`.
- **`PROVEN` state:** once the audit data clears the expectancy bar, sizes
  normally. If the data proves negative expectancy, the mode tells the user to
  stop firing it (mirrors the existing per-setup net-negative calibration callout).
- **Caps:**
  - per-trade risk from `profile.py` (0.5% default) — unchanged;
  - the **whole ladder counts as one position** against that cap (inventory cap —
    laddering cannot silently multiply risk);
  - **session shot-cap** (max N passive scalps per session) so "many shots"
    cannot become overtrading.

### 4.4 Behavioral integration (`behavioral.py`)

- Passive fades are NOT directional chases → entries with
  `setup_family: passive-fade` are **excluded** from FOMO-streak / R16 leak
  counting (otherwise normal scalp cadence would falsely read as tilt).
- New **passive tilt guard**: N consecutive passive scalps losing ⇒ the range is
  breaking ⇒ informational cooldown on *fading* (same FYI-not-HALT posture as the
  current cooldown).

### 4.5 Audit + replay

- `audit_log.py`: add `setup_family` field; `summary` breaks out passive-fade
  win-rate / expectancy separately. **This is the data source for the §4.3 gate.**
- `replay.py`: learn the to-the-mean exit + ladder fill + time-stop so passive
  decisions are counterfactually scored like everything else.

### 4.6 Output format

Compact passive block: `regime_label` + `fade_ok`; the bid/ask ladders; mean
target; hard stop; the three invalidation rules; `PROVEN | UNPROVEN` expectancy
status. TINY one-liner for `/loop`. Suppress JOURNAL STUB on no-action (consistent
with existing no-action outputs); show it when a passive fade actually arms.

---

## 5. Invocation

- `/scalp passive <COIN>` — full read (default COIN = HYPE)
- `/scalp tiny passive <COIN>` — one-liner / `/loop`
- `/scalp passive <COIN>` with no `fade_ok` → prints regime label + "sit out" reason
- Existing `/scalp <COIN>` and `/scalp short <COIN>` unchanged.

## 6. Key risk (make-or-break)

Fade-to-mean scalping is **negatively skewed**: many small wins, occasional large
losses when a "range" was actually a breakout and the passive limit filled
*because* the level broke (textbook adverse selection). The high win rate masks
the skew. The design's defense is layered:
1. `fade_ok` keeps the user out of about-to-break ranges (classifier quality is
   paramount — hence §7 backtest gate);
2. microprice gate disarms ladders leaning into a break;
3. hard structural stop + quick-pull bound the left tail;
4. the expectancy gate refuses to size up until the audit log *proves* the net
   edge is positive.

If the classifier cannot reliably distinguish ranging from about-to-break tape in
backtest, **the mode should not ship** — the strategy has no edge without it.

## 7. Validation gates (before trusting the mode)

- Extend `backtest_thresholds.py` to validate `regime` thresholds
  (`range_compression`, `btc_corr`, two-sided-flow cutoffs) against forward
  outcomes — chosen values must sit on a plateau, not a spike.
- Unit tests for the regime classifier (known-input → expected label/`fade_ok`).
- Replay tests for the passive exit simulation (ladder fill, to-mean exit,
  time-stop, quick-pull).

## 8. Files touched (phased, ≤5 files/phase per working rules)

1. `fetch_market.py` — `regime` block (classifier)
2. `scalp-passive.md` — new module
3. `scalp-core.md` + `SKILL.md` — wire `/scalp passive` invocation + routing note
4. `audit_log.py` — `setup_family` field + summary breakout
5. `behavioral.py` — passive carve-out + tilt guard
6. `replay.py` — passive exit simulation
7. `tests/` — regime classifier unit tests + passive replay tests
8. `backtest_thresholds.py` — extend to validate regime thresholds

## 9. Open questions / deferred

- Exact numeric thresholds for `range_compression`, `btc_corr`, two-sided-flow,
  ladder steps, time-stop bars, session shot-cap, `UNPROVEN→PROVEN` sample size
  and expectancy bar — to be set during implementation, **backtest-driven**, not
  guessed.
- Mean definition for the exit target (microprice vs range-mid vs session VWAP) —
  pick one in implementation; default candidate: session VWAP, fallback range-mid.
- Reconcile `.claude/skills/scalp` vs canonical `research/scalp` (skills dir is
  not a symlink) — housekeeping, not blocking.
