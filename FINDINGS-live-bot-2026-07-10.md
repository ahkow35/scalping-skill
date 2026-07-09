# Deterministic Scalp Bot — Findings & Methodology (for external review)

Date: 2026-07-10 · Repo: `ahkow35/scalping-skill` · Branch/PR: `feat/deterministic-live-bot` (PR #1)
Author: build session. Conclusion under review: **the deterministic scalp
triggers have no generalizable edge; do not deploy.**

This document is written to be *attacked*. Section 6 lists exactly what a
skeptic should check and what could overturn the conclusion.

---

## 1. Objective

Turn the existing (LLM-driven, interactive) `/scalp` skill into an unattended,
deterministic program that watches a coin and alerts on trade setups — with **no
LLM in the decision path** (fixed rules → fast, free, and *testable*). Before
deploying, prove the setups have positive expectancy. That proof step is what
this document reports; it returned negative.

Constraints fixed up front (not under review): alert-only (no order execution),
HYPE long+short, VPS-hosted loop + Telegram, dashboard deferred.

---

## 2. System under test (all committed, 218 unit tests passing)

The decision path was ported from the skill's prose spec into four pure-Python
modules. A reviewer should verify the port faithfully reflects the intended
setups (see §6, item 1).

| Module | Responsibility | Key logic |
|---|---|---|
| `structure.py` | Support/resistance from candles | N-bar fractal pivots → ATR-scaled clustering → "tested" level = ≥2 touches; sweep-reclaim / sweep-rejection flags |
| `triggers.py` | The 4 entry setups | long: A sweep-reclaim, B momentum-break; short: A failed-breakout, B lower-high reject. Each emits entry/stop/T1/T2 + gross & net R:R; geometry- and R:R-guarded |
| `decide.py` | Orchestrator → verdict | behavioral halt → macro veto → structure+triggers → flow conviction cuts → sizing |
| `backtest.py` | Walk-forward expectancy tester | reuses `replay.py`'s fill/stop/target simulator; two lenses (param sweep, noise injection) |

Shared two-timeframe helper `structure.with_entry_sweeps` lets a fast (5m)
sweep of a slow (15m) level fire an entry — used identically by backtest and
the (unbuilt) live path, so backtest == live.

---

## 3. Methodology

### 3.1 Walk-forward
At each closed bar *i*, structure + triggers are computed on candles `[:i+1]`
only; if a trigger fires **and** clears the net-R:R admission floor, the trade
is simulated on candles `[i+1:]`. No look-ahead: `swing_pivots` excludes the
unconfirmed tail, and the fill sim never sees the signal bar. A cooldown
suppresses re-counting the same setup on adjacent bars.

### 3.2 Fill simulation (reused from `replay.py`, already unit-tested)
- Entry modeled as a **market fill at the signal-bar close** (a synthetic
  at-entry candle is prepended so the sim starts FILLED).
- Ladder: 50% out at T1 (stop→breakeven), 50% at T2; mark-to-market if the
  window ends open.
- **Costs charged on every fill**: `TAKER_FEE 0.00045 + SLIPPAGE 0.0002` per
  fill × 2 fills, converted to R. Tighter stops cost more R (this is why a
  "profitable" zero-cost backtest can be net-negative live).
- Same-candle stop/target conflicts resolve **stop-first** (conservative; no
  1-minute drill used here).

### 3.3 What it measures, and the key assumption
The backtest measures **raw trigger expectancy** — it does *not* apply the
live-only gates (flow/taker-delta, behavioral state, regime) because those
caches only build live and cannot be reconstructed historically.

**Assumption (challengeable — see §6.2):** the live gates only ever *cut* or
*shrink* trades, never add them, so raw expectancy bounds the gated system's
*exposure*. Precise statement: a negative raw expectancy is not automatically
fatal *iff* the untestable flow/regime selection has strong skill at removing
losers — but that is an unfalsifiable rescue, and (per §5) the signal fails to
generalize across coins even before gating, which coin-agnostic gates could not
repair.

### 3.4 Fixed parameters (not exhaustively swept — see §6.3)
pivot window 2/2; cluster band 0.5×ATR; min_touches 2; stop_buffer 0.25×ATR;
net-R:R floor 2.0; cooldown 4–6 bars; horizon 48×15m (Exp 1) / same-UTC-day
(Exp 3–4). `stop_buffer` and the R:R floor *were* swept (Exp 1, 2).

---

## 4. Experiments & results (real Hyperliquid candles)

### Exp 1 — Baseline, 90d HYPE, 15m entry, 12h hold, floor 2.0
| Trigger | n | win% | net exp | total |
|---|---|---|---|---|
| long_B | 31 | 25.8 | −0.48R | −14.8R |
| short_A | 12 | 16.7 | −0.75R | −9.0R |
| short_B | 13 | 15.4 | −0.54R | −7.1R |
| long_A | 0 | — | never qualified | — |

Robustness: widening `stop_buffer` walks the loss toward zero from below (long
−0.95→−0.13R as 0.15→0.5) — *not* a positive plateau. Noise injection degrades
expectancy smoothly from a negative baseline (the negative is real, not a
parameter spike). → **NO-GO on this configuration.**

### Exp 2 — Is the R:R floor too strict? Floor sweep, 90d HYPE, 15m
| Floor | LONG n / win / exp | SHORT n / win / exp |
|---|---|---|
| 0.0 | 766 / 33.8% / −0.47R | 879 / 38.6% / −0.30R |
| 1.0 | 138 / 32.6% / −0.68R | 55 / 30.9% / −0.41R |
| 2.0 | 31 / 25.8% / −0.48R | 23 / 13.0% / −0.69R |
| 3.0 | 9 / 22.2% / +0.14R | 12 / 8.3% / −0.68R |

Loosening does **not** help: taking everything (floor 0) loses −359R/−259R over
766/879 trades. Win rate never climbs enough as targets come in (peaks 34–39%,
needs >50% for a ~1:1 trade). Conclusion: the problem is **entry timing, not
risk management**. (The floor-3.0 long +0.14R is n=9 noise.)

### Exp 3 — Timeframe correction: 15m levels / 5m entry / same-UTC-day close
The Exp 1 setup (15m entry, 12h hold) was a *swing* system, not a scalp. The
skill specifies 5m entry and same-day exit. Faithful test, 60d HYPE, floor 2.0:

| Setup | n | win% | net exp |
|---|---|---|---|
| long_A (flush-reclaim) | 7 | 42.9 | **+0.88R** |
| long_B (momentum-break) | 54 | 35.2 | −0.28R |
| short_A | 23 | 17.4 | −0.71R |

LONG pool improved from −0.48R (Exp 1) to −0.15R. `long_A` — which *never fired*
on 15m — flips positive. **A lead worth validating**, but n=7 is far too small
to trust.

### Exp 4 — Out-of-sample validation of long_A across coins (intraday, floor 2.0)
| Coin | long_A n / win / exp / total | long_B exp |
|---|---|---|
| HYPE (in-sample, 90d) | 9 / 33.3% / **+0.40R** / +3.6R | −0.29R |
| SOL (60d) | 3 / 0.0% / **−1.58R** / −4.7R | −0.85R |
| INJ (60d) | 6 / 16.7% / **−1.03R** / −6.2R | −0.50R |
| NEAR (60d) | 14 / 28.6% / **−0.03R** / −0.4R | −0.60R |
| **fresh pooled (SOL+INJ+NEAR)** | **23 / 21.7% / −0.49R / −11.3R** | negative |
| **all-4 pooled long_A** | **32 / ~−0.24R** | — |

`long_A` is positive on **exactly the one coin it was discovered on** and
negative-to-breakeven on three fresh coins. This is the signature of
**overfitting**: the HYPE result was coin-specific noise, not edge.

---

## 5. Conclusion

Every deterministic trigger is net-negative to breakeven, out-of-sample, on
every fresh coin, on the faithful intraday timeframe. The single positive result
(long_A on HYPE) does not generalize. **No generalizable edge exists in these
triggers as specified → do not deploy the live bot.** The build (engine +
harness) is retained as reusable tooling; the trade *signal* is not there.

Secondary finding: unfiltered trading (floor 0) loses ~10× more than the gated
system (Exp 2), i.e. the skill's value is its **discipline/gating layer**
(keeping you out of bad trades), not entry alpha.

---

## 6. Limitations — what a skeptical reviewer should check

1. **Faithfulness of the port.** `structure.py`/`triggers.py` are *my*
   interpretation of the skill's prose (`scalp-long.md`, `scalp-short.md`). If a
   trigger is mis-specified (e.g. entry-on-retest vs entry-at-signal, or a
   stop-placement rule), the negative could be an artifact of the port, not the
   strategy. **Highest-priority check.** Diff the modules against the skill docs.
2. **The "raw expectancy" assumption (§3.3).** The backtest omits the flow /
   behavioral / regime gates. I argue they can only reduce exposure, not create
   coin-generalizable edge — but this is the load-bearing methodological claim.
   A reviewer may argue a strong flow filter selectively keeps winners. Note it
   is historically untestable here (no taker-delta history).
3. **Parameter coverage.** `stop_buffer` and R:R floor were swept; pivot window
   (2/2), cluster band (0.5×ATR), and min_touches (2) were **not**. A different
   structure parameterization could in principle surface edge. Not exhausted.
4. **Sample sizes are small for the intraday setups** (long_A n=3–14 per coin).
   The OOS *direction* is consistent (negative on all fresh coins), but each
   per-coin estimate has a wide error bar.
5. **Window inconsistency in Exp 4.** HYPE ran 90d; SOL/INJ/NEAR ran 60d (the
   first 4-coin run hit an API rate-limit; the retry used 60d + throttling).
   Windows should be equalized for a clean comparison — easy to redo.
6. **Single era / four coins.** All data is crypto ~mid-2026 (a chop-to-down
   regime for HYPE). A different regime or a broader coin set could differ.
7. **Fill model optimism/pessimism.** Market-fill-at-close is mildly optimistic
   (no gap risk on entry); stop-first conflict resolution and full round-trip
   costs are pessimistic. Net direction of bias is small but not zero. No 1m
   drill was used for same-candle conflicts.
8. **Validation orchestration scripts** (`validate.py`, `intraday.py`,
   `rr_sweep.py`) currently live in the session scratchpad, not the repo — the
   *core* they call (`backtest.run_intraday`, `structure`, `triggers`, `replay`)
   is committed and unit-tested, but the orchestration is not yet version-
   controlled. Should be committed for full reproducibility.

**What would overturn the conclusion:** a corrected port (item 1) that surfaces
positive OOS expectancy; or a structure re-parameterization (item 3) that does
the same across coins. Absent one of those, the conclusion stands.

---

## 7. Reproduction

```bash
# unit tests
python3 -m pytest -q                         # 218 passing

# Exp 1 baseline + robustness lenses
python3 backtest.py --coin HYPE --days 90 --side both --sweep --noise

# Exp 2 R:R floor sweep — see rr_sweep.py (loops backtest.run over floors)
# Exp 3/4 intraday + OOS — see intraday.py / validate.py
#   (call backtest.run_intraday(side, c5, c15, allow=('A',), trig_params={'min_net_rr_t1': f}))
```
Data: `backtest.fetch_history(coin, interval, days, now_ms)` pages Hyperliquid
`candleSnapshot` (throttle for multi-coin to avoid HTTP 429).

---

## 8. Test coverage
`structure` 19 · `triggers` 12 · `decide` 9 · `backtest` (strategy+intraday) 10,
plus the pre-existing suite = **218 tests**. Covers: pivot detection, level
clustering, sweep flags, each trigger's fire/no-fire + geometry + R:R guards,
the orchestrator's verdict paths (HALT/VETO/WAIT/action + sizing), and the
walk-forward (finds trades, empty on flat, cooldown, same-day stop, allow-list).

## Appendix — commit map (branch `feat/deterministic-live-bot`)
- `30834bc` structure engine + plan
- `fc21190` trigger state machine
- `cc74105` decision orchestrator
- `34934d3` backtest harness (Phase 3, first NO-GO)
- `e95f771` intraday two-timeframe path
- `713a288` final verdict (this analysis) in `PLAN-live-bot-2026-07-09.md`
