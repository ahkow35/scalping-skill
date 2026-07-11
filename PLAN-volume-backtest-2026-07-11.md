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
