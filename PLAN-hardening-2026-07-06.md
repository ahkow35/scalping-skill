# PLAN — Scalp skill hardening (2026-07-06)

Origin: two independent reviews (2026-07-06). Every finding below was verified
against the working tree before planning: behavioral.py read in full, fee
handling grepped in replay.py/audit_log.py, test suite run (151 passed, but the
two `assemble` tests hit the live Hyperliquid API — 0.27–0.42s vs <5ms for all
others; they pass only when the sandbox allows that host).

Priority order per the reviews: daily stop → live net-R:R → spread/depth gate.
Test-isolation fix goes first because it is mechanical and restores hygiene
before behavior changes.

Standing constraints: `test_skill_consistency.py` must pass after every md
edit; each phase ends with a checkpoint commit; thresholds get backtested
against the audit log before a gate goes hard (funding-veto precedent,
lesson 2026-06-10).

---

## Phase 0 — Test isolation fix (mechanical)

**Bug:** `assemble()` calls `_post_json` directly for core perps
(fetch_market.py:555), bypassing the monkeypatched `fetch_ctx` in
`test_assemble_marks_macro_unavailable_when_btcd_fails` (tests:166) and
`test_assemble_attaches_regime_block` (tests:379). Both hit live DNS.

- [x] Extract the core-perps `metaAndAssetCtxs` fetch into a module-level
      `fetch_core_meta()` helper; `assemble()` calls it.
- [x] Both tests monkeypatch `fetch_core_meta` with a fake universe
      (primary coin + BTC).
- [x] Acceptance: full suite green; no unit test exceeds ~50ms (proxy for
      "no network").

## Phase 1 — Daily-loss circuit breaker (HARD gate)

**Gap:** behavioral.py only surfaces the *last* resolved loss, and
scalp-core.md Step 0a makes it informational-only. Nothing ever stops a
losing day. Strongest consensus rule in day-trading doctrine (prop-firm
daily max loss).

- [x] behavioral.py: new `daily_stop` block, keyed off `resolved_at_ms`:
      active when EITHER (a) sum of resolved `outcome_r` in trailing 24h
      ≤ −2.0R, OR (b) the last 2 consecutive directional resolved trades
      each ≤ −0.7R within trailing 24h. Expose `{active, until_utc,
      realized_r_24h, reason}`. Active until 24h after the qualifying
      resolve (rolling).
- [x] Backtest BEFORE wiring: score against the audit log — how often would
      it have fired, and what did the blocked window do (replay `net_r`)?
      Tune the −2.0R default if the data disagrees. Report findings first.
- [x] Backtest result: current audit log has only 1 resolved trade; 0 daily-stop
      activations and 0 would-block windows. Threshold not tunable yet; keep
      conservative default and revisit after enough resolves.
- [x] scalp-core.md Step 0: cooldown stays informational; `daily_stop` is
      HARD — ENTRY action verdicts forced to `HALT (daily stop)`; MANAGE
      unaffected (managing an open position must never be blocked).
      TINY form: `<COIN> HALT | daily stop <−X.X>R (until <T>)`.
- [x] Known limitation, documented in the md: the gate is only as timely as
      resolves. Stale unresolved action entries blunt it — `/scalp summary`
      already nags; add the nag to the HALT line.
- [x] SKILL.md description + consistency lint + tests (test_behavioral.py).

## Phase 2 — Shared cost model + net R:R at trade admission

**Gap:** replay.py charges taker fee + slippage (net_r) and audit summary
prefers net_r, but the LIVE admission rule ("skip any setup with R:R < 2:1",
Step 6c) and the output templates are gross-only. The live system can fire a
setup replay would score sub-threshold. Constants currently live only in
replay.py.

- [x] New `costs.py`: `TAKER_FEE`, `SLIPPAGE_FRAC`, `COST_PER_FILL`,
      `cost_r(entry, stop)`, `net_rr(entry, stop, target)`. Single source
      of truth; replay.py imports from it and deletes its local copies.
- [x] scalp-core.md 6c: the floor becomes NET — skip if net R:R < 2:1 on T1
      AND net weighted-across-scales < 2.5:1.
- [x] Trigger template prints both: `RR <gross> (net <net>)` on the trigger
      line; TINY keeps gross for brevity but the admission check is net.
- [x] Audit payload: add `net_rr_t1` / `net_rr_t2` next to the gross fields
      (additive — no schema break).
- [x] Tests: costs.py units + replay parity test (identical scores before/
      after the extraction).

## Phase 3 — Spread / slippage / depth execution gate

**Gap:** spread is computed and displayed but never gated; no size-vs-depth
check. Step 6d explicitly never blocks on missing microprice (keep that).

- [x] Live `cost_r` per trigger from costs.py + `book`: (round-trip fee +
      half-spread + slippage) ÷ stop distance.
- [x] New Step 6e rules: `cost_r` > 0.20R → maker-only entry (post limit,
      never market-take); > 0.35R → additionally downgrade one conviction
      tier + flag `⚠ cost-heavy`. Missing/suspect book → current fallback
      unchanged (never block a fired trigger on missing data).
- [x] Depth: intended size > ~25% of visible top-3-level depth on the entry
      side → cut size to that cap + flag.
- [x] All three thresholds marked PROVISIONAL in the md — revisit after 20
      resolved trades carry cost data.

## Phase 4 — VWAP + OI context (added 2026-07-07)

Both land READ-ONLY (regime-classifier precedent): surfaced as context,
zero verdict/conviction effect until backtested.

- [x] `compute_vwap()` — UTC-day-anchored VWAP from 1h hlc3×vol candles;
      `out['vwap']` with side/dev_bps. Passive mode's mean is now this
      deterministic value (was LLM-computed prose).
- [x] OI history cache `.oi_cache.jsonl` (btcd-cache pattern — HL only
      exposes current OI): `update_oi_cache` / `compute_oi_change` (1h ±15m,
      24h ±90m tolerance; price change window-aligned from the same cached
      sample) / `classify_oi_read` (new-longs, short-covering, new-shorts,
      long-unwind, flat; noise thresholds 0.5% OI / 0.3% price PROVISIONAL).
- [x] Output: VWAP on the Range line, OI line after FLOW-GATE, warming
      forms; direction modules document the OI×price doctrine + `fighting
      VWAP` flag; consistency lint extended to guard all of it.
- [x] Promotion criterion: backtest VWAP-side and OI-read conditioning once
      candle-history depth allows (HL API currently truncates long windows);
      only then may either signal cut conviction.

## Deferred backlog (explicitly NOT this run)

HTF-alignment check in QUICK; re-entry-after-stop cap; directional
trades-per-day soft cap; default directional time-stop; token-unlock /
crypto-native event calendar.

## Verification per phase

pytest suite green → consistency lint green → md/SKILL.md in sync →
checkpoint commit (`git status` first, stage only this session's files).
Phase 1 additionally: backtest report reviewed before the gate goes hard.
