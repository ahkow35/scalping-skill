# Volume-Filter Backtest — Pre-Registration & Plan

Date: 2026-07-11 · Branch: `feat/volume-filters` · Status: PRE-REGISTERED (written before any results)

## Hypothesis

The four deterministic triggers (NO-GO per `FINDINGS-live-bot-2026-07-10.md`)
never used volume. Volume-conditioned admission may select the subset of
signals with real participation and flip expectancy positive. This is the last
untested cheap variation before the trigger family is abandoned for the
shadow-bot brief.

## Configuration (frozen — identical to Exp 4b except where stated)

- Coins: HYPE (development) + SOL, INJ, NEAR (out-of-sample), 60d each.
- Timeframes: 15m levels / 5m entry / same-UTC-day close (faithful path,
  `backtest.run_intraday`).
- Entry modes: close AND retest.
- Costs: unchanged (taker 0.00045 + slippage 0.0002 per fill).
- Conflict resolution: stop-first (unchanged).
- RVOL definition: signal-bar volume ÷ rolling mean of prior 20 bars on the
  same timeframe (5m entry bar; exclude the signal bar from the mean; guard
  divide-by-zero → RVOL undefined → trade excluded from binned analysis, not
  counted as passing any filter).

## Analyses (in order; A1 is primary)

### A1 — RVOL-quartile diagnostic (primary, floor 0.0 for power)
For every trigger fire at floor 0.0 (population from Exp 2 methodology, run
intraday), record signal-bar RVOL and simulated net R. Bin by RVOL quartile
per (trigger, side). Question: is expectancy monotone in RVOL at all?
If A1 shows no monotone (or inverse) relationship for a trigger, threshold
filters on that trigger are declared dead regardless of A2 cell results.

### A2 — Threshold filters at floor 2.0 (confirmatory)
Filter families:
- **F1 participation:** admit only if signal-bar RVOL ≥ k, k ∈ {1.5, 2.0, 3.0}.
  Applies to all four triggers.
- **F2 climax absorption:** for sweep triggers (long_A, short_A) only — the
  *sweep* bar (not the reclaim/rejection bar) RVOL ≥ k, k ∈ {1.5, 2.0, 3.0}.

48 cells total (4 triggers × 2 modes × F1 k3) + (2 triggers × 2 modes × F2 k3).
Expected false positives at loose standards: ~2–3. Hence:

### Pass criteria (a cell PASSES only if ALL hold)
1. Net expectancy > 0 on HYPE.
2. Pooled SOL+INJ+NEAR net expectancy ≥ 0 with pooled n ≥ 30.
3. A1 quartile trend for that trigger is directionally consistent (top
   quartile ≥ bottom quartile in net R).
4. Neighbouring k values do not flip sign wildly (no isolated spike).

Anything failing any criterion is reported but REJECTED. No post-hoc filter
variants may be added after seeing results; a second pre-registration would be
required.

### A3 — Report-only diagnostics (no pass/fail)
- Expectancy by RVOL for the floor-0 population split by session (Asia/EU/US)
  — seasonality confound check on the rolling-20 RVOL definition.
- n and win% per cell, MAE/MFE where available.

## Deliverables

1. Volume filter implemented as an **admission-layer condition in the backtest
   path** (not a rewrite of `triggers.py` internals). Parameterized, defaulted
   OFF so existing behaviour and tests are untouched.
2. Analysis orchestration **committed to the repo** (lesson from
   FINDINGS §6.8 — no scratchpad-only scripts): `analysis_volume.py`.
3. Unit tests for RVOL computation (incl. zero-volume guard, window edge) and
   filter admission logic.
4. Results appended to this file under `## Results` with the pass-criteria
   verdict per cell, plus a one-paragraph conclusion.

## Interpretation commitments (written before results)

- All cells fail → trigger family is abandoned for the shadow bot; signal
  candidates reduce to (b) passive fade / (c) validated-components composite.
- 1–2 cells pass marginally → treated as suggestive only; requires a second
  fresh-coin validation (e.g. ARB/SUI) before being frozen for shadow.
- Broad consistent pass (same filter direction across triggers/coins) →
  strongest case; freeze exact params and proceed to shadow-bot spec.

---

## Results (2026-07-12, run after pre-registration; full tables in `analysis_volume_run.log`)

Note on cell count: the pre-registration prose above says "48 cells" but its own
formula gives (4×2×3) + (2×2×3) = **36**. The analysis runs the formula as
written; 36 is the correct count. (short_B has no F2 — it is not a sweep
trigger — and short_B never clears the floor-2.0 admission intraday anyway,
consistent with Exp 4b.)

### A1 — RVOL-quartile diagnostic (primary; floor 0.0, close mode, 60d × 4 coins)

**No trigger shows a monotone RVOL→expectancy relationship.** Pooled across
coins (n=964 long_A, 699 long_B, 1026 short_A):

| Trigger | Q1 (lowest RVOL) | Q2 | Q3 | Q4 (highest RVOL) |
|---|---|---|---|---|
| long_A | −0.27R | −0.28R | −0.24R | **−0.30R** |
| long_B | −0.67R | −0.73R | −0.59R | −0.61R |
| short_A | −0.32R | −0.46R | −0.32R | **−0.42R** |
| short_B | n=0 — never fires intraday | | | |

The highest-volume quartile is *slightly worse* than the lowest for long_A and
short_A. Expectancy is negative in **every quartile of every trigger on every
coin** (48 quartile cells, zero positive pooled). Under the pre-registered A1
rule, threshold filters are dead for all triggers before A2 is even consulted.

### A2 — Threshold filters (confirmatory; floor 2.0, close+retest)

**36 cells, 0 PASS.** Every cell fails at least criteria 1 (HYPE exp > 0) and
3 (A1 monotone consistency); most also fail 2 (pooled OOS ≥ 0 with n ≥ 30).
The single positive-expectancy cell (long_B close F1 k3.0, pooled OOS +0.44R)
is n=16 with HYPE n=2 at −2.16R — an isolated sub-threshold spike, exactly the
class of lucky cell the pass criteria were pre-registered to reject.

Anomaly noted for review: long_A close-mode admits n0 on HYPE / n1 pooled OOS
at floor 2.0 under any RVOL filter — sparse but directionally consistent with
Exp 4b's n15 pooled unfiltered.

### A3 — Session split (report-only)

Expectancy negative in all sessions for all triggers. Long setups are least
bad in Asia (00–08 UTC) and worst in US hours; mean RVOL is similar across
sessions (1.5–1.9), so the rolling-20 RVOL definition is not badly
session-confounded.

### Review (2026-07-12, independent reviewer — verdict PASS)

The NO-EDGE conclusion was independently attacked and held: RVOL computation,
F2 sweep-bar identification, and admission wiring verified clean (no
look-ahead/leakage); the long_A close n0 anomaly reproduced as genuine market
data (the unfiltered floor-2.0 population is itself n0 on HYPE this window).
Findings actioned:
1. **Monotonicity classifier was stricter than pre-registered criterion 3**
   (demanded a full Q1..Q4 staircase; plan requires only top ≥ bottom). Fixed
   post-review to the plan's literal criterion. Reviewer verified by direct
   rerun that no A2 cell flips in this run (criterion 1 fails every cell
   independently); the fix matters only for reruns. The tables above retain
   the original run's stricter labels.
2. **Process gap:** the criteria-encoding script was committed together with
   results, not before the run. No evidence of cherry-picking (the defect was
   uniformly over-strict), but future pre-registered studies must commit the
   analysis script BEFORE execution.
3. **Ambiguity noted:** under retest entry mode, RVOL is measured on the
   trigger-detection bar, not the (later) fill bar — defensible, now explicit.

### Conclusion

Volume-conditioned admission does **not** rescue the deterministic triggers.
The primary diagnostic shows volume carries no information about these
setups' outcomes at any threshold — the failure mode is the entry logic
itself, not missing participation confirmation. Per the pre-registered
interpretation commitment: **the trigger family is abandoned for the shadow
bot**; remaining signal candidates are (b) the regime-gated passive fade and
(c) a validated-components composite. This closes the "last cheap variation"
question raised in the 2026-07-11 review of the shadow-bot brief.
