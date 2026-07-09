# PLAN — Deterministic live scalp bot (loop + notify + dashboard)

Status: APPROVED — decisions locked · 2026-07-09 · repo `ahkow35/scalping-skill`

## Decisions locked (2026-07-09)

1. **Compute** — VPS-B (Kinna box), systemd timer. Stateful caches run unchanged.
2. **Notify/UI** — **Telegram-only for v1.** The here.now dashboard + all
   `state.json` / Site Data / VPS-endpoint plumbing is **deferred** (Phase 5
   cut from v1 scope).
3. **Watchlist** — HYPE long + short only, 5m cadence.
4. **Execution** — alert-only. No order placement, no exchange keys, no custody.

Net effect vs. the original draft: v1 is Phases 0–4 only; no dashboard, no
external state store, no Site Data write to verify.

## Phase 3 result (2026-07-09) — NO-GO

The backtest gate FAILED. Over 90 days of real HYPE 15m, every deterministic
trigger is net-negative (raw expectancy, the conservative lower bound):
- `long_B` momentum-break: n=31, win 25.8%, **-0.48R**
- `short_A` failed-breakout: n=12, win 16.7%, **-0.75R**
- `short_B` lower-high reject: n=13, win 15.4%, **-0.54R**
- `long_A` sweep-reclaim: never qualified in 90d (R:R floor too strict, or the
  pattern is genuinely rare — flagged for review).

Robustness lenses confirm the negative is real, not a tuning artefact: widening
the stop only walks the loss toward zero from below (trade-less-lose-less, not a
positive plateau), and noise injection degrades expectancy smoothly from a
negative baseline. **Phase 4 (VPS deploy) is BLOCKED** — deploying a
negative-expectancy signal would lose money. The engine + harness remain valid,
reusable IP; the triggers-as-specified do not have demonstrable edge on HYPE.

Open decision: iterate the triggers using the now-existing harness (best lead:
a historically-computable regime filter — `regime.py` is candle-only for
compression/directionality/BTC-corr), or shelve.

## Final verdict (2026-07-10) — SHELVE. No generalizable edge.

Two diagnostics ran after the first NO-GO:
1. **R:R floor sweep** (0→3.0): loosening the floor does NOT help — taking
   everything loses catastrophically (−359R/−259R over 766/879 trades). Win
   rates never climb enough as targets come in (34–39% peak, need >50%). The
   problem is **entry timing, not risk management**. No R:R setting rescues it.
2. **Timeframe correction** (the 15m/12h test was wrong — a swing system, not a
   scalp). On the faithful **15m-levels / 5m-entry / same-day-close** path,
   `long_A` (flush-reclaim) flipped from never-fires to positive on HYPE
   (+0.40R, n9) — a lead worth validating.

**Out-of-sample validation KILLED the lead.** long_A across 4 coins (60–90d
intraday):
- HYPE (in-sample): +0.40R (n9)
- SOL −1.58R (n3), INJ −1.03R (n6), NEAR −0.03R (n14)
- pooled fresh coins: **−0.49R (n23)**; all-four pooled **~−0.24R (n32)**

long_A was positive on exactly the one coin it was found on → overfitting, not
edge. Every trigger is net-negative to breakeven on every fresh coin.

**DECISION: shelve the live bot.** The deterministic triggers as specified have
no generalizable edge; automating them would systematically lose money. Phases
4–5 are cancelled. What survives as reusable IP: `structure.py`, `triggers.py`,
`decide.py`, `backtest.py` (+ intraday), and — most valuable — a validated
methodology that caught the overfit BEFORE any capital was risked. The skill's
real value was always the GATING (macro veto, behavioral discipline, R:R floor,
sizing) that keeps you OUT of bad trades, not alpha generation. The backtest
proves it: unfiltered trading (floor 0) loses ~10× more than the gated system.

## Goal

Run the scalp analysis unattended all day, and get pushed a notification the
moment a real trade opportunity fires — **without an LLM in the decision loop**.
The LLM stays optional and out-of-band (explain-an-alert only). Alert-only:
the bot never places orders.

## The core realisation

~80% of the skill is already deterministic Python (`fetch_market`, `regime`,
`flow`, `vwap`, `costs`, `behavioral`, `profile`, `audit_log`, `replay`). Two
things currently live only in the LLM's head and must be ported to code:

1. **Structure** — where the floor/ceiling/mid are, "tested ≥2×", sweep+reclaim.
2. **Triggers** — "swept the ceiling, closed back below → fire", with entry/stop/target.

Everything else the loop needs (gates, conviction cuts, sizing, audit) already
returns structured JSON. And `replay.py` already contains the fill simulator we
need for backtesting. So this is mostly two new modules + wiring, not a rewrite.

## Architecture — four boxes, LLM-free hot path

```
[VPS-B: scalp_loop.py]  --every 3–5m-->  decide() --action verdict?-->  Telegram push (you)
        |  (reuses local .jsonl caches, unchanged)                 |
        |                                                          v
        +--> writes latest snapshot + alert --> [state store] --> [here.now dashboard PWA]
```

- **Engine** — `scalp_loop.py` on VPS-B. Deterministic. State-change-gated
  exactly like the skill's `/loop` TINY rules (silent on unchanged WAIT,
  heartbeat every 30 min, escalate on action verdict). Auto-logs every
  decision through the existing `audit_log.py`, so `/scalp summary`,
  `/scalp replay`, and calibration keep working.
- **Notify** — Telegram bot (primary, storage-independent). Fires only on a
  state change into an action verdict or a MANAGE event.
- **State store** — local JSONL/SQLite on VPS-B for the engine (no change);
  a small `state.json` pushed to the dashboard surface. **No Supabase.**
- **Dashboard** — here.now static PWA, password-gated, reads the snapshot.
  Shows levels, sizing math, R:R, and the R16 discipline checklist. Secondary —
  the Telegram alert already carries the decision.

## Compute decision (given your constraints)

**Recommendation: VPS-B (the Kinna Hetzner box), systemd timer every 3–5 min.**

The engine is inherently *stateful* — it maintains four rolling caches
(`.trade_cache/*.jsonl`, `.oi_cache.jsonl`, `.btcd_cache.jsonl`, the audit log)
that grow across runs and ARE the signal (taker-delta coverage, BTC.D 24h
change, OI change all need persistent history). A box with a persistent
filesystem that's already always-on and already paid for is the correct home;
the current cache code runs unchanged.

| Option | Verdict | Why |
|---|---|---|
| **VPS-B systemd timer** | ✅ recommended | Persistent fs = caches work as-is. Zero marginal cost, zero re-architecture. |
| Railway free | ⚠ fallback | Persistent worker is possible, but always-on tips past the $5 trial credit and volumes for the caches are a paid add-on. OK only if you won't touch VPS-B. |
| Vercel cron | ❌ not for engine | Serverless = ephemeral fs → all four caches must be externalised to KV first. Real re-architecture. (Vercel Pro is fine for the *dashboard* if you ever want it off here.now.) |

## Storage decision (no Supabase)

- **Engine state**: stays local on VPS-B — JSONL caches unchanged, plus one
  **SQLite** file for structured alert/backtest history (free, right scale).
- **Dashboard data**: engine writes a compact `state.json` (latest verdict per
  watched coin/side + last N alerts). Path, in preference order:
  1. Push to **here.now Site Data** via its authenticated write route; PWA reads
     it. Keeps everything in here.now + VPS. *(Verify Site Data supports a
     server-side write — flagged as an open item.)*
  2. Fallback: a tiny read-only `GET /state` on VPS-B behind a bearer token,
     reverse-proxied by the web server VPS-B already runs. PWA fetches it.
- **Telegram** needs no store — it's the reliable primary channel regardless.

## Reuse vs. new

**Reuse unchanged:** `fetch_market.py`, `regime.py`, `flow.py`, `vwap.py`,
`costs.py`, `behavioral.py`, `profile.py`, `audit_log.py`, and — critically —
`replay.py`'s fill/stop/target simulator for the backtest.

**New modules (all under `research/scalp/`):**
- `structure.py` — swing/pivot detection. In: 1h/15m/5m candles. Out:
  floors, ceilings, mid pivot, classification (range/breakdown/trend/
  failed-breakout), sweep+reclaim / sweep+rejection flags. Method: n-bar
  fractal pivots → cluster within an ATR-epsilon band → touch count (≥2 =
  "tested"). Generalises to BTC candles for the macro structural-break veto.
- `triggers.py` — per-level state machine. Long: A sweep-reclaim, B
  momentum-break. Short: A failed-breakout, B lower-high rejection. Tracks
  {watching → swept → reclaimed/rejected → FIRED} with 5m-close confirmation;
  emits entry/stop/targets; computes gross + net R:R via `costs.py`.
- `decide.py` — the orchestrator that ports what the LLM does across core +
  direction modules into one function: fetch → behavioral gate → macro veto →
  structure → triggers → flow/regime conviction cuts (Step 1b) → sizing →
  verdict object (same shape as the audit payload). This is the single source
  of truth the loop and the backtest both call.
- `backtest.py` — run `decide()`'s structure+trigger logic over historical
  candles, pipe every fired trigger through `replay.py`'s simulator, report
  per-trigger/side expectancy. Follows `backtest_thresholds.py` rigor:
  parameter-plateau sweeps + `--noise` injection (a real edge degrades
  smoothly). **This is the go/no-go gate before anything goes live.**
- `scalp_loop.py` — the daemon: for each watched coin/side call `decide()`,
  diff against previous state, emit Telegram + snapshot on action-state change,
  heartbeat, log to audit. Config-driven (watchlist, cadence, sides).
- `notify.py` — Telegram push (token from env on VPS-B, never in code/chat).
- `dashboard/` — here.now static PWA + the `state.json` writer.

**New tests (match existing suite):** `test_structure.py`, `test_triggers.py`,
`test_decide.py`, extend `test_backtest.py`.

## What stays human — deliberately

- **R16 behavioral preflight** (are you tilted / chasing / calm?) is about you,
  not the market. The bot runs the *automatable* gates (daily-stop + cooldown
  from `behavioral.py`) but cannot assess psychology. It ships as a **checklist
  in the alert** you tick before acting. Feature, not gap.
- **No auto-execution.** v1 alerts only — no order placement, no exchange API
  keys, no custody surface. Matches the skill's philosophy and your risk posture.

## Phasing (each phase independently reviewable; ≤5 files)

- **Phase 0** — scaffolding: config schema (watchlist/cadence/sides), env
  handling for the Telegram token, SQLite schema. 1 PR.
- **Phase 1** — `structure.py` + `triggers.py` + tests, **TDD** (load-bearing
  logic). Validate the detector against hand-labelled historical windows.
- **Phase 2** — `decide.py` orchestrator + tests. Parity check: run over the
  last ~30 days, confirm fired triggers map to real sweep/reject events and
  agree with any audit rows in-window.
- **Phase 3** — `backtest.py`. Expectancy per trigger/side with plateau +
  noise checks. **HARD GATE: go live only if expectancy is positive and
  robust.** This is the first time we'll actually know if triggers A/B pay —
  today there are zero resolved trades, so this is the real payoff.
- **Phase 4** — `scalp_loop.py` + `notify.py` on VPS-B (systemd timer).
  End-to-end Telegram alerting works here, no dashboard needed yet.
- **Phase 5 — DEFERRED (not in v1).** here.now dashboard PWA + `state.json`
  plumbing. Revisit after Telegram alerting is proven.

Phases 0–3 are pure local Python in the repo → build-to-PR pipeline applies.
Phase 4 touches VPS-B (systemd unit, Telegram token) → I'll pause for your go
before deploying (system-state + outbound gates).

## Decisions — RESOLVED (see top of doc)

All five forks answered 2026-07-09: VPS-B compute, Telegram-only (dashboard
deferred), HYPE long+short @ 5m, alert-only. Telegram is the notify channel.

## Risks / caveats

- Rule-based structure detection is slightly worse than a trained eye at "is
  this *really* a failed breakout." Phase 3 backtest measures the cost; Phase 1
  hand-labelling tunes it. Reproducibility + backtestability is the trade we're
  buying.
- here.now Site Data server-write is unverified — fallback endpoint noted.
- Backtest is component/mechanical, not live P&L proof (same honesty as
  `backtest_thresholds.py` and `replay.py`). Positive backtest = permission to
  paper-run, not a guarantee.
```
