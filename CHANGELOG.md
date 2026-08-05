# Changelog — scalp skill

## 2026-08-06 — post-mortem-driven behavioral gates (coin lockout, tilt-coin guard, MANAGE time-stop)
Full-fill post-mortem of the real Hyperliquid account (11,944 fills, 2025-01 →
2026-08: net −$254k, of which −$384k across three days — ETH short capitulation
2025-08-22, DOGE long liquidation 2025-10-10, DOGE re-entry unwind 2025-12-31)
showed all three blowups shared one mechanism: scalp-sized thesis → multi-day
leveraged hold with no stop → re-entry into the same losing coin+side.
Counterfactual replay on the actual fills: a −$5k daily circuit breaker saved
$425k / forfeited $0 (16/16 tripped days never recovered); a same-coin+side
lockout blocked a −$346k pattern. Encoded: `behavioral.py` `coin_lockout`
(2 losses ≤ −0.5R same coin+side within 12h → 24h hard ENTRY block; MANAGE
proceeds; passive fades excluded), scalp-core Step 0e tilt-coin guard (DOGE:
lifetime −$228.5k → conviction capped at med, in-plan only, until 20 resolved
DOGE trades show positive expectancy), Step M position-age time-stop (>24h =
forced close-or-swing decision; >72h underwater = stale-hold callout), and the
0a daily stop pinned with its empirical basis. Additive dict key — card2.py /
decide.py consumers read via `.get()`, unaffected. 6 new tests; suite 265
green. Known limit (recorded in lessons): gates see only skill-logged trades;
real-fill API integration listed as an open item.
Files: behavioral.py, scalp-core.md, SKILL.md, tests/test_behavioral.py.
Post-mortem: vault raw/hyperliquid-perp-post-mortem-2025-2026.md.

## 2026-07-31 — NO-TRADE lines name the binding macro condition
A vetoed coin printed only `macro veto blocks <lean>`; the causes lived in
`macro["flags"]`, which `build_cards` forwarded on the behavioral-cooldown and
session-stop branches but dropped on the per-coin veto branch — so a live
NO-TRADE couldn't be read without reconstructing the gate by hand (hit during a
manual `/scalp2 HYPE` where the cause turned out to be BTC 1h breakdown).
`macro_gate` now returns `veto_reasons: {"long": [...], "short": [...]}` and
derives `veto_long`/`veto_short` from those lists, so state and text can't
drift; the veto branch appends the matching side's causes. Incidental: the four
`flags` strings are generated from one source instead of duplicated, and the
BTC funding flag now says "BTC funding" — it reads BTC's funding but said just
"funding", colliding with the per-coin `own funding` veto message below it.
`build_cards` reads the key via `.get()`, so a macro dict without it degrades
to the old bare line rather than raising. Full suite 259 passed; ruff unchanged
at 9 pre-existing errors (lambda assignments in tests). Verified live: HYPE
scan now prints `macro veto blocks long — BTC 1h structural breakdown`.
Files: card2.py, tests/test_card2_gates.py.

## 2026-07-30 — regression eval harness for decide() (PR #2, merged)
Added `evals/` — a golden-file regression suite for the deterministic decision
engine. `fetch_market.py` now appends every run's full market dict to
`evals/snapshots.jsonl` (gitignored firehose; file-only sink, stdout untouched,
cannot break a live fetch). `evals/bless.py` freezes a snapshot's current
`decide()` output as a committed golden case; `evals/run_regression.py` replays
`golden/*.json` through the current `decide()` and deep-diffs vs frozen
`expected` (ignores human-text `reason`), non-zero exit on mismatch. Wired into
pytest via `tests/test_evals.py` (+ `tests/fixtures/market_hype.json`): 4 tests
— bless roundtrip, fresh-set passes, corrupted verdict fails, reason ignored.
Full suite 259 passed (255 + 4). Files: fetch_market.py, .gitignore, evals/*,
tests/test_evals.py, tests/fixtures/market_hype.json.
KEY FINDING: no fetch→decide adapter needed — `fetch_market` output is a direct
superset of decide()'s market contract (a live spike disproved an earlier grep
that suggested `btc_ctx`/`macro_can_clear`/`btc_dominance` were absent). Scope:
regression-tests the deterministic engine (the port of the LLM's logic), NOT the
live interactive /scalp LLM verdict — that's a separate future eval. Golden set
is curated, not exhaustive; seeded with 2 real HYPE WAIT cases, fills as varied
snapshots accrue and get blessed. Merged via feature-branch PR (merge commit
15eaa9c); local main fast-forwarded.

## 2026-07-27 — scalp2 merged to main + activated
Branch feat/scalp2-scanner merged (8 commits, 255 tests). Final Opus
whole-branch review caught 3 cross-module defects fixed pre-merge:
funding veto was wired to BTC not the traded coin; session-stop/evidence
gates had no ENTRY writer (added `scan2.py log-entry`); single-coin fetch
failure aborted whole scan (now per-coin isolated). Activated with user
approval: crontab 20:55 SGT weekdays + skill symlink
`.claude/skills/scalp2` → `skill/scalp2`. Cron pinned to Python 3.14
after system 3.9 reproducibly failed on HL responses (IncompleteRead);
verified under cron-equivalent minimal env. Equity $5,000 confirmed;
fees base tier (user-confirmed). Live checkout moved
fix/flow-honesty → main (scan2.py must be on the checked-out branch for
cron; WIP branches preserved).

## 2026-07-26 — scalp2 scanner (feat/scalp2-scanner)
Session-open scanner per spec 2026-07-24: universe2/card2/scan2 + thin
/scalp2 skill. Scanner claims no entry alpha (deterministic cards, human
decides); reuses validated v1 gates. Risk 0.5%/attempt proving-phase,
evidence gate at 40 resolved. Cron 20:55 SGT weekdays (pending user approval).
v1 skill untouched. Rejected: greenfield rewrite (re-pays hardened
lessons); strip-v1-in-place (can't reach lightweight by subtraction).

## 2026-07-10 — Deterministic live-bot investigation → SHELVED (no edge)

**Summary:** Investigated turning the skill into an unattended deterministic
bot (VPS loop + Telegram alerts, no LLM in the decision path, alert-only).
Built the full engine + backtest harness, then the go/no-go gate returned a
robust NO-GO: the triggers have no generalizable edge. Not deployed.

- **New modules:** `structure.py` (fractal pivots → ATR-clustered tested
  levels + sweep flags), `triggers.py` (the 4 setups as pure functions with
  gross/net R:R + geometry guards; close-vs-retest entry variants),
  `decide.py` (behavioral → macro veto → structure/triggers → flow cuts →
  sizing → verdict), `backtest.py` (walk-forward + intraday 15m-levels/5m-entry
  same-day path, reusing `replay.py`'s fill simulator; param-sweep + noise
  lenses). `structure.with_entry_sweeps` shares the two-timeframe logic so
  backtest == live. +37 tests → 222 green.
- **Result:** every trigger net-negative to breakeven OOS across HYPE/SOL/INJ/
  NEAR, both entry modes. The one apparent lead (HYPE long_A +0.40R) was a
  stop-placement bug artifact — collapsed to n1/−1.40R once fixed.
- **External review** corrected 3 port mis-specs (long_B ceilings-only; long_A
  structural-pool stop or skip; retest entries); the rerun *strengthened* the
  veto (commit `889d733`).
- **Decision:** SHELVE the live bot. Engine + harness + methodology retained as
  reusable tooling. The skill's value is its discipline/gating layer, not entry
  alpha (unfiltered trading loses ~10× more than the gated system).
- **Docs:** `PLAN-live-bot-2026-07-09.md`, `FINDINGS-live-bot-2026-07-10.md`
  (reviewer-facing), `analysis_rr_sweep.py`, `analysis_intraday_oos.py`.
- **Branch:** `feat/deterministic-live-bot` (PR #1) — merged to main 2026-07-10
  (merge `4ea2ca0`) as reference tooling; not wired into the live `/scalp`
  skill, alert-only, never executes orders.

## 2026-07-07 — VWAP + OI context (READ-ONLY Phase 1)

**Summary:** The two top deferred-backlog items from the July reviews, landed
read-only per the regime-classifier precedent — context only, zero
verdict/conviction effect until backtested.

- **VWAP:** `compute_vwap()` — deterministic UTC-day-anchored VWAP (1h
  hlc3×volume) in `out['vwap']` with side + dev_bps. Shown on the Range line;
  direction modules flag `fighting VWAP` entries as context. Passive mode's
  mean is now this value instead of LLM-computed prose.
- **OI:** new rolling `.oi_cache.jsonl` (btcd-cache pattern — Hyperliquid only
  exposes current OI). `out['oi']` carries 1h/24h OI + window-aligned price
  change and a classic OI×price read (new-longs / short-covering / new-shorts /
  long-unwind / flat; noise thresholds provisional). Direction modules document
  the doctrine per side. `read: null` = cache warming.
- **Tests:** VWAP/OI unit tests + assemble isolation for the new cache write +
  consistency-lint guards. Suite green at 169 passing, no network.
- **Files:** fetch_market.py, scalp-core.md, scalp-long.md, scalp-short.md,
  scalp-passive.md, SKILL.md, .gitignore, tests.

## 2026-07-06 — Hardening: daily stop, net R:R, spread/depth guard

**Summary:** Closed the top risk gaps from the July reviews.

- **Test isolation:** factored core-perps `metaAndAssetCtxs` into
  `fetch_core_meta()` so assemble tests patch the network boundary instead of
  hitting live Hyperliquid DNS.
- **Daily stop:** `behavioral.py` now emits `daily_stop`, active on trailing
  24h realized R ≤ -2.0R or two consecutive directional losses ≤ -0.7R. The
  skill treats it as a hard ENTRY `HALT (daily stop)` while MANAGE remains
  allowed. Backtest against the current audit log found only 1 resolved trade,
  so thresholds are conservative and not yet tunable.
- **Net R:R:** added shared `costs.py`; replay imports the same fee/slippage
  constants as live admission. Directional trigger admission is now net of
  trading costs, and audit trigger payloads include `net_rr_t1` / `net_rr_t2`.
- **Spread/depth:** fetcher now exposes top-3 L2 depth. Step 6e adds provisional
  cost-heavy execution rules and a 25% top-3 depth size cap.
- **Tests:** full suite green at 160 passing.

## 2026-06-29 — Noise-injection robustness test (Varma §7)

**Summary:** Added the input-noise robustness lens to `backtest_thresholds.py`
— the complement to the existing parameter sweeps. A sweep asks "is the
threshold on a plateau?"; this asks "is the edge even real?" by perturbing input
prices with rising random noise and checking the edge metric degrades smoothly.

- **`backtest_thresholds.py`** — `add_noise` (per-bar multiplicative O/H/L/C
  noise, seeded/deterministic), `noise_robustness` (curve of edge vs sigma,
  reps-averaged, with per-level std), `degradation_verdict` (PASS = smooth
  decay toward 0; SUSPECT = jagged / strengthens under noise), and two edge
  metrics: `atr_protective_edge`, `regime_separation_edge`. New `--noise` CLI.
- **Tests** — +7 (`test_backtest.py`): identity at sigma 0, H/L bracket
  preserved, determinism, curve shape, PASS/SUSPECT/INSUFFICIENT verdicts.
  **158 passed.**
- **Live run (HYPE, 365d, 5000 1h bars, 8 reps):**
  - **regime gate** — separation real & stable at low noise (+0.0058,
    std ~0.0004 at sigma≤0.001); SUSPECT flag is driven by a high-noise reading
    within ~1 std (measurement variance, not a true spike). Read: **likely
    robust, borderline** — don't curve-fit `compression_quiet`; more reps to
    confirm.
  - **ATR veto (long side)** — base effect tiny and WRONG-SIGNED (-0.0017):
    **no robust long-protective edge on HYPE.** Actionable: the long-side ATR
    veto isn't earning its keep here (may only protect shorts / needs rework).

## 2026-06-28 — Volume / order-flow confirmation gate (Step 1b)

**Summary:** Encoded Varma's "volume is the key signal" heuristics
([[samir-varma-react-to-risk-quant-trading]] §8a) as a deterministic classifier
+ a conviction gate. Volume now has to CONFIRM a move or conviction is cut.

- **`flow.py`** (new, mirrors `regime.py` — pure, never raises): from candles
  (per-bar volume) + bucketed `taker_delta` it emits `coverage_ok`,
  `aggressor_bias`, `volume_climax` (capitulation/blow-off), `delta_divergence`
  (price-up-on-selling / price-down-on-buying = exhaustion), `breakout_vol_ok`.
  Climax/breakout baselines **adapt to available bars** (fetch returns ~19 5m
  bars) down to a `min_bars` floor.
- **`fetch_market.py`** — attaches `out['flow']` next to `out['regime']`.
- **`scalp-core.md` Step 1b** (new) — maps the flow read to conviction; it only
  ever **cuts** (volume confirms an edge or it doesn't, never manufactures one):
  low coverage → cap low; bias opposing the side → −2; balanced → −1; divergence
  against side → −1; climax in your direction (you're late) → −1; unconfirmed
  breakout → −1. Cuts stack; drives WAIT when low + weak macro. New FLOW-GATE
  output line; optional FLOW line on WAITs. SKILL.md coverage note updated.
- **Tests** — +11 (`test_flow.py`), incl. short-history adaptation. **151 passed.**
- Live smoke test: gate computes end-to-end on HYPE.

## 2026-06-28 — Realistic-cost counterfactuals (replay net of fees + slippage)

**Summary:** Counterfactual replay now scores trades **net of trading costs**,
closing the one gap that made expectancy untrustworthy (it previously filled at
trigger price with zero cost — flagged in the [[samir-varma-react-to-risk-quant-trading]]
review, Varma: "your system is signal + entry + exit — model all three").

- **`replay.py`** — added a cost model: `TAKER_FEE` (0.045%) + `SLIPPAGE_FRAC`
  (2 bps) per fill, `ROUND_TRIP_FILLS=2.0`, and `round_trip_cost_r(lv)` which
  converts cost to R as `2 × COST_PER_FILL × entry/risk` — so **tighter stops
  cost more in R**. `simulate_trigger` now returns `net_r` + `cost_r` alongside
  gross `r` (gross is unchanged, so the fill-logic unit tests still pin it).
  `apply_replay_to_entry` adds `net_best_r` + a `cost_model` block.
- **`audit_log.py`** — `_counterfactual_stats` aggregates NET (`net_best_r` /
  per-trigger `net_r`) with fallback to gross for pre-cost counterfactuals.
- **Tests** — +6 (cost math, net-of-cost outcomes, unfilled pays nothing,
  summary prefers net). Full suite **140 passed**.
- **Re-scored the live log (`--force`):** 47 scored. Net result — cost drag
  **0.108R/trade avg** (median 0.097R, max 0.197R), eating **~5R of ~24.5R
  gross (≈20%)**. Edge survives in simulation (long-B +1.03R/n15, long-A
  +0.70R/n13) but this is COUNTERFACTUAL, net, still optimistic on *whether*
  limit fills happen — only **1 real resolved trade** exists. Prove with live
  resolves before sizing up. Tune `TAKER_FEE`/`SLIPPAGE_FRAC` to real tier.

## 2026-06-18 — Passive flow-capture mode (Plan 2: 2A + 2B + 2C)

**Summary:** Built `/scalp passive <COIN>` — a both-sides mean-reversion fade
gated by the Plan 1 regime read and an audit-log expectancy check. Ships at
UNPROVEN (quarter-size) until ≥40 resolved passive trades prove a net edge
(else STOP). Three committed phases:

- **2A (`57111d9`)** accounting/risk foundation: `by_setup_family` summary
  breakout; `passive_expectancy()` UNPROVEN/PROVEN/STOP gate (min_samples=40,
  quarter-size until proven) + CLI; behavioral carve-out (passive excluded from
  directional FOMO) + `passive_tilt` guard.
- **2B (`04f342b`)** `scalp-passive.md` trade module (regime/expectancy/event/
  tilt gates → range edges → bid/ask limit ladders → exit-to-mean → 3-way
  invalidation → expectancy-gated sizing + session shot-cap) + `/scalp passive`
  routing.
- **2C (this commit)** right-sized: replay SKIPS passive-fade entries (they
  resolve manually; the directional ladder sim would mis-score them). The full
  passive simulator was dropped as over-engineering — see PLAN-phase2 §2C.
- **fetch fix (found by running `/scalp passive` once):** the 15m candle window
  was 2.5h (~10 bars), but the regime classifier needs ≥20 (compression) / ≥32
  (btc_corr) — so production regime was ALWAYS "unknown" and passive `fade_ok`
  NEVER armed (the feature was inert). Widened the primary + BTC 15m windows to
  12h (~48 bars) in `fetch_market.py`. Live `/scalp passive HYPE` now returns a
  real regime read (e.g. quiet → SIT-OUT, EXPECTANCY UNPROVEN ×0.25).

Full suite 136 passing. No directional behavior changed.

**Caveat:** the §7 gate was validated on 1h candles; production computes
compression/correlation on 15m. The compression sweep was inert (label is
directionality-driven), so the threshold transfers, but the 15m structural read
is not independently backtested — the expectancy gate remains the real arbiter.

**Decisions:**
- Expectancy bar raised 20→40 samples (a ~75% breakeven win rate makes 20 too
  noisy to trust a PROVEN flip).
- Edge-proximity left as an LLM prose rule, not code (floor/ceiling are
  judgment); add a helper only if soak shows drift.
- Live edge remains UNPROVEN by design — the gate must earn PROVEN on real
  resolved trades; it may instead resolve to STOP (a cheap, correct kill).

## 2026-06-17 — Regime classifier foundation (passive-mode Plan 1) + §7 gate

**Summary:** Built the deterministic market-regime read (the missing "directional
edge is poor in quiet/correlated tape" half of the order-flow thesis) as Plan 1
of a passive flow-capture mode. Ships READ-ONLY: it surfaces a WEATHER line but
does not change any verdict yet. Backtest-validated as the gate before Plan 2
(the passive-fade trade machinery) is built.

**Files (all changes uncommitted/working-tree — commits held by operator):**
- `regime.py` (new) — pure classifier: `true_ranges`, `range_compression`,
  `directionality`, `btc_corr`, `two_sided_flow`, `classify` → `regime_label`
  ∈ {trending, ranging, quiet, correlated-chop, unknown} + coarse `fade_ok`.
- `fetch_market.py` — fetch BTC 15m candles (for correlation); attach `out["regime"]`.
- `scalp-core.md` — read-only WEATHER line in QUICK + TINY output templates.
- `backtest_thresholds.py` — `regime_forward_stats` + `regime_compression_sweep`
  + `--regime` CLI (neutral-flow, trailing-window structural validation).
- `tests/test_regime.py` (new, 16) + additions to `test_fetch_market.py`,
  `test_skill_consistency.py`, `test_backtest.py`. 123 tests, all passing.
- `tests/test_fetch_market.py` — isolated `test_assemble_marks_macro_unavailable...`
  from the real on-disk BTC.D cache (operator chose test-isolation over fixing
  the latent negative-age fallback bug in uncommitted btcd code).

**§7 GATE RESULT — PASS (qualified).** `--regime --coin HYPE --days 180` (4321×1h):
- What it shows: ranging-labelled tape has LOW forward drift (trending
  mean_abs_fwd 0.0215 vs ranging 0.0149; signed drift +0.0068 vs +0.0012). This
  proves the NECESSARY-not-sufficient half — "ranging tape doesn't trend, so a
  fade won't get steamrolled." It does NOT prove fades are profitable: low drift
  ≠ mean-reversion (a random walk also has ~0 net drift). The backtest grouped
  by regime label and measured forward-return magnitude; it never conditioned on
  being AT an edge and measured return-to-mean (the actual fade edge). That edge
  is unprovable from candles (no flow history) — the live expectancy gate is the
  real arbiter.
- Compression sweep 0.4→0.8: separation flat at ~0.0066 (robust plateau, not a
  spike) — but compression_quiet is nearly INERT; directionality does the work.
- CAVEAT: structural gate is permissive — 80% of bars label "ranging" (n=3436)
  vs trending 2% (n=98). Real selectivity must come from the un-backtested FLOW
  gate (tsf) + Plan 2's edge-proximity fade_ok + the expectancy/min-size gate.
  The classifier separates regimes correctly; it is not by itself selective.

**Decision:** Gate criteria met → Plan 2 is justified. But live edge stays
UNPROVEN until the audit log earns it (by design). Flow validation is live-only
(no multi-day taker history), so the backtest validates structure only.

## 2026-06-10 — Counterfactual replay engine + threshold backtest

**Summary:** Closed the audit-log feedback loop. WAIT/VETO decisions are now
scored against what subsequently happened (replay engine), and the mechanical
macro-veto thresholds were backtested against ~208 days of HYPE history.

**Files:**
- `replay.py` (new) — counterfactual simulator: fill/stop/T1-breakeven/T2
  ladder, short mirror via price negation, same-candle conflict drill-down
  to 1m with conservative fallback, chunked HL candle fetch, CLI.
- `backtest_thresholds.py` (new) — component backtest of ATR-spike and
  funding-extreme veto conditions vs forward returns/MAE, threshold sweeps.
- `audit_log.py` — `attach_counterfactual`, GATE VALUE summary section,
  open-entry semantics exclude counterfactually-closed non-action rows.
- `SKILL.md` — `/scalp replay` admin command, GATE VALUE summary block.
- `tests/test_replay.py`, `tests/test_backtest.py` (new) — 29 tests
  (82 total, all passing).

**Decisions:**
- Forward test + counterfactual scoring is the evaluation methodology;
  classic backtest only for mechanical sub-components (signal layer is
  judgment + live-only data).
- Replay conventions: fill at trigger price, 50% T1 → BE stop, 50% T2,
  conservative ambiguity resolution, 72h window.
- Findings: funding-extreme veto validated (protective from 0.03%/8h);
  ATR down-bar veto refuted as drift signal (marks MAE risk, not
  continuation); WAITs missed +2.5R, VETOs blocked +4.1R across the first
  13 logged decisions. Threshold changes proposed, NOT yet applied to
  protocol files.

Commits: `167aa02`, `fc5a386`.

## 2026-06-11 — Threshold changes applied (Nyan sign-off)

- Funding-extreme veto tightened ±0.05 → ±0.03%/8h (both direction modules
  + SKILL.md), per backtest: protective effect clear from 0.03.
- BTC 2× ATR spike demoted from hard veto to size modifier (half size,
  wider structural stop, tighter time-box) in both modules: backtest showed
  it marks adverse-excursion risk, not directional continuation.
- Files: scalp-long.md, scalp-short.md, SKILL.md.

## 2026-06-13 — Profile/state layer + instruction-consistency lint

Acted on external review (5 of 8 items; #6 deferred, #7 rejected).

- **profile.py** (new) — `.scalp_profile.json` {equity, phase} + `/scalp
  profile` command. Sizing math now sources equity/cap deterministically;
  unblocks /loop (no more asking for equity every run).
- **behavioral.py** (new) — cooldown / FOMO-streak / OOP-this-week derived
  from the audit log instead of conversation memory. Step 0 preflight reads
  it; works headless under /loop.
- **audit payload** — `plan_status` captured so the weekly OOP cap persists.
- **SKILL.md / scalp-core.md** — frontmatter drift fixed (five admin
  commands; replay/profile/quick in triggers); journal-stub contradiction
  resolved (log all QUICK, show stub on action verdicts only).
- **tests/test_skill_consistency.py** (new) — lints command/frontmatter
  consistency + no blanket journal-stub mandate; catches the drift class.
- 22 new tests (104 total).

Deferred: econ-dates file (#6). Rejected: openai.yaml (#7, wrong ecosystem).
