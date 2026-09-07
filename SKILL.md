---
name: scalp
description: >
  Live tactical quant scalp thesis and position management for any Hyperliquid
  perp (default HYPE), gated by a BTC + BTC-dominance macro veto, a behavioral
  preflight, a BTC-beta (strip-BTC) idiosyncrasy check, real taker-aggressor
  delta, and UTC/SGT session awareness.
  Long AND short setups, each with its own protocol module. Three output sizes —
  TINY (one-line pulse, default for /loop), QUICK (standard), DEEP (full).
  Two modes — ENTRY (fresh thesis) and MANAGE (review open position).
  Plus six admin commands — `account` (read-only account risk and stop audit), `summary` (audit log aggregate stats),
  `resolve` (close out an open trade with outcome), `list-open` (list unresolved
  entries), `replay` (counterfactual scoring of logged decisions), `profile`
  (set account equity / phase for sizing). Trigger phrases: "/scalp",
  "/scalp account check", "/scalp <COIN>", "/scalp tiny <COIN>", "/scalp quick <COIN>",
  "/scalp deep <COIN>", "/scalp short <COIN>", "/scalp tiny short <COIN>",
  "/scalp deep short <COIN>", "/scalp manage [short] <COIN> <entry>",
  "/scalp tiny manage [short] <COIN> <entry>", "/scalp summary [--since-days N]",
  "/scalp resolve <trade_id> <R> <exit_reason> [lesson]", "/scalp list-open",
  "/scalp replay [--window-h N] [--force]", "/scalp profile [set <field> <value>]",
  "scalp read", "scalp thesis", "scalp the tape", "review my <COIN> long",
  "review my <COIN> short", "manage my position",
  "what's the move now" (when a position is open).
---

# Scalp

Activating the live tactical scalp companion.

## Step 0 — Admin commands (short-circuit before trading flow)

Check args FIRST. If the first token is one of these, run the admin command
and STOP — do NOT proceed to the trading flow, do NOT fetch market data, do
NOT run behavioral preflight.

### `/scalp account check`

For `/scalp account check`, run `python3 account_monitor.py check` from the
skill repository and report its status and limitations; then STOP. For account
configuration, read `ACCOUNT-MONITOR.md` and obtain the public trading wallet
and owner-selected daily loss limit. Never request a private key or place orders.

### `/scalp summary [--since-days N]` (audit journal)

Run:
```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py summary [--since-days N]
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py list-open
```

Both return JSON. Render as the block below — do NOT dump raw JSON. The
list-open call is used only to count stale entries (>72h open on action
verdicts); individual entries belong in `/scalp list-open`, not here.

```
SCALP AUDIT — last <N> days  (or "all-time" if --since-days omitted)
──────────────────────────────────────
Total entries: <total>
Resolved: <resolved> (open: <open>)

Verdict distribution:
  <verdict>  <count>  (<pct>%)
  ... one row per verdict, sorted by count desc, pad to align

Performance by setup (resolved only):
  Setup <name>: <n> trades, win rate <pct>%, expectancy <±R>R, total <±R>R
  ... sorted by total_r desc
  (if no resolved trades yet: "No resolved trades yet — log outcomes via /scalp resolve")

GATE VALUE (counterfactual replay, <scored> scored):
  WAITs:  <n> scored, <fired> would have filled — missed <±R>R
  VETOs:  <n> scored, <fired> would have filled — blocked <±R>R
          (negative = the gate saved you; positive = the gate cost you)
  By setup (simulated): <side>-<label>: n <n>, win <pct>%, expectancy <±R>R
  ... sorted by total_r desc; omit whole block if scored == 0
  [if unscored open non-action entries exist: "→ run /scalp replay to score <K> new entries"]

Open entries: <open_count> unresolved.
  [if any stale >72h on action verdicts: "⚠ <K> stale action entries — see /scalp list-open"]
```

Then append calibration callouts if any of these conditions are TRUE
(each one prints on its own line, omit if not triggered):
- If `(VETOED / total) > 0.5` → `⚠ VETO rate is <pct>% — macro gate may be mis-tuned`
- If `(WAIT / total) > 0.7` → `⚠ WAIT rate is <pct>% — caller may be over-patient`
- If `(LONG-NOW + SHORT-NOW) / total < 0.05` AND total >= 20 → `⚠ Action verdict rate is <pct>% — bar may be too high`
- If any setup has `n >= 5` AND `expectancy_r < 0` → `⚠ Setup <name> is net-negative (expectancy <R>R over <n> trades) — review whether to keep firing it`

### `/scalp resolve <trade_id> <outcome_r> <exit_reason> [lesson]`

Run:
```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py resolve <args>
```

If the script returns 0, confirm to the user with the trade_id and outcome.
If it returns 1 (not found / already resolved), say so plainly and offer to
list open entries.

### `/scalp replay [--window-h N] [--force]`

Counterfactual scorer — replays every open logged decision against the candles
that followed it: did the trigger fill, and did the stop or targets get hit
first? Run:
```bash
python3 /Users/nyanyk/Claude/research/scalp/replay.py [--window-h N] [--force]
```

Returns JSON `{scored: [...], skipped: [...]}`. Render one line per scored
entry: `<trade_id>  <verdict>  best_r <±R>  (<per-trigger statuses>)`, then
the skipped list compactly. After rendering, run `/scalp summary` mechanics
to show the updated GATE VALUE block. Conventions (state them if asked): fills
at trigger price with no slippage — slightly optimistic; same-candle
stop/target conflicts drill to 1m and otherwise resolve conservatively as
stop-first with `ambiguous: true`; 50% out at T1 → stop to breakeven, 50% at
T2 (the 20% trail is approximated by the T2 exit); window default 72h,
mark-to-market if still open at window end. Real `/scalp resolve` outcomes
always take precedence — replay never touches resolved entries.

### `/scalp list-open`

Run:
```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py list-open
```

Render as a compact table:

```
OPEN AUDIT ENTRIES
──────────────────────────────────────
<trade_id>  <coin> <side>  age <h>h  verdict <V>  conviction <C>  [⚠ stale if action verdict + age > 72h]
... one row per entry, sorted by age desc
```

For action verdicts (LONG-NOW/SHORT-NOW) older than 72h, append "⚠ stale"
and remind the user to resolve.

If the list is empty: `No open audit entries — everything's closed out.`

### `/scalp profile [set <field> <value>]`

Account state the sizing math needs but cannot infer (equity, phase). With no
args, show current profile:
```bash
python3 /Users/nyanyk/Claude/research/scalp/profile.py get
```
To update: `python3 .../profile.py set equity 5000` or `set phase 2`. Fields:
`equity` (USDC, > 0), `phase` (1 → 0.5% default cap, 2 → 2.0%). Render the
returned JSON as one line: `equity $<E> | phase <N> (default cap <X>%)`. If
equity is null, say so and prompt the user to set it — the trading flow needs
it before any action verdict can show sizing.

## Step 1 — Determine direction, then load the protocol

- Long (default): args do NOT contain `short` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-long.md`.
- Short: args contain `short` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-short.md`.
- Passive: args contain `passive` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-passive.md`. (Overrides long/short:
  passive mode fades both sides.) Invoked as `/scalp passive <COIN>` or
  `/scalp tiny passive <COIN>`.

`scalp-core.md` is the direction-neutral protocol (Steps 0,1,5,6,M, output
and journal skeletons, /loop). The direction module supplies Steps 2–4 and
the output deltas. Read core FIRST, then the direction module.

## Step 2 — Pick the mode

- **ENTRY mode** (default): no open position context → fresh thesis,
  Steps 0–6, QUICK/DEEP output.
- **MANAGE mode**: user is in a position ("review my long/short", stated
  entry, "what's the move now" referencing an earlier fill) → Step M,
  MANAGE output. Direction comes from the stated position; load the
  matching direction module.
- **TINY mode**: `tiny` in args (or default for `/loop` monitoring) →
  one-line pulse, ENTRY or MANAGE depending on position context. Use for
  high-frequency monitoring without flooding the screen.

If args include `manage <COIN> <entry_price>`, force MANAGE mode. If args
include `short`, the position/thesis is short. If args include `tiny`,
force TINY output regardless of ENTRY/MANAGE.

## Step 3 — Execute it exactly
Follow `scalp-core.md` + the direction module exactly.
- Default = QUICK (ENTRY) or MANAGE format.
- `tiny` in args = TINY mode (one-line, ENTRY or MANAGE).
- `deep` in args = DEEP mode (ENTRY only).
- `passive` in args = PASSIVE mode (loads scalp-passive.md; both-sides fade).
- `/loop` invocations default to TINY unless QUICK/DEEP is explicit.
- Coin arg defaults to HYPE.
- **Account preflight runs before every new entry**, including passive mode:
  run `python3 account_monitor.py check --json` from the skill repository.
  Only a fresh `entry_allowed: true` permits further entry checks. Missing
  configuration, unsupported account mode, errors and unknown data block new
  entries. Report the exact status; do not infer CLEAR from silence or a cached
  result. MANAGE and protective analysis must still proceed. Read
  `ACCOUNT-MONITOR.md` for scope, partial-day baselines and stop limitations.
  This read-only latch cannot block manual trades or place/cancel orders.
- **Step 0 Behavioral preflight runs after account preflight** (daily stop + cooldown + R16 vibe
  check + coin lockout + tilt-coin guard). Read state from `python3
  behavioral.py` (derived from the audit log — works under /loop with no
  conversation). `daily_stop.active` in ENTRY mode → HARD `HALT (daily stop)`
  before market fetch; MANAGE still proceeds. `coin_lockout` matching the
  requested coin+side (2 material losses ≤ −0.5R within 12h → 24h lock) →
  HARD `HALT (coin lockout)` in ENTRY mode, MANAGE proceeds. Tilt-coin list
  (currently DOGE) → conviction capped at med, in-plan only. Cooldown
  active → print `BEHAVIORAL WARNING: cooldown active (...) — informational
  only` and PROCEED with full analysis (no halt, no verdict/conviction effect).
  R16 leaks → conviction penalty + named warning; trade proceeds. MANAGE mode
  surfaces position age: >24h = action-required time-stop decision.
- Macro veto runs SECOND, from the direction module. Includes hard
  NO-TRADE on FOMC/CPI/NFP/PCE days + extreme funding (long: >+0.03%/8h;
  short: <-0.03%/8h).
- Risk cap + equity come from `python3 profile.py get` (equity, phase →
  default cap). If equity is null, ask once and offer to save via
  `/scalp profile set equity <amt>`; a per-trade A+ override to 1% is still
  declared inline. Flag the cap when non-default.
- Every trigger block shows the sizing math (equity × cap = $risk;
  $risk ÷ stop_distance = position size; leverage derived, not chosen).
- Directional triggers must pass the NET R:R floor from `costs.py`; show gross
  and net R:R in QUICK/DEEP trigger blocks.
- JOURNAL STUB is shown for ACTION verdicts (LONG-NOW / SHORT-NOW) and
  MANAGE-action-required only. No-action outputs (WAIT/VETOED/NO-TRADE/HALT
  and compact MANAGE) are still audit-logged but suppress the stub.
- Passive-mode entries set `setup_family: "passive-fade"` in the audit payload
  (directional entries omit it / default "directional").
- ALWAYS run the Step 1b volume/flow gate from `out['flow']` (coverage,
  aggressor bias vs side, climax, divergence, breakout-volume) — it can only
  CUT conviction. `coverage_ok == false` (max coverage <50%) caps conviction
  at low: you're scalping half-blind (Varma §8a).
- ALWAYS run the Step 1c strip-BTC (idiosyncrasy) gate by reading the
  deterministic `out['strip_btc']` (`strip_btc.py`) — CUT-only: if
  `out['strip_btc'].cut` is true (`status == "beta"`: high BTC corr, move aligned
  with BTC, not outrunning it) cut conviction one tier and print the STRIP-BTC
  line with its `reason`; otherwise no effect. Do not re-derive by eye. Strip the
  shared market factor before trusting a directional read (partial-correlation
  logic).
- Apply Step 6e spread/depth guard when L2 is available: cost-heavy books force
  maker-only or conviction downgrade; intended size is capped to 25% of visible
  top-3 entry-side depth. Missing book data does not block a fired trigger.
- Surface the VWAP and OI context lines from `out['vwap']` / `out['oi']`
  (READ-ONLY Phase 1 — they never change verdicts or conviction; `fighting
  VWAP` and the OI×price read are context flags only). Passive mode uses
  `out['vwap'].vwap` as its deterministic mean.
- Short module only: apply the weekend modifier when
  `session.weekend_window` is true (size ×0.5).
- Never inline-curl; only use `fetch_market.py` output.
- Never estimate a number that came back `DATA UNAVAILABLE`.

## Note on taker_delta cache
The fetch script builds a local trade cache at
`/Users/nyanyk/Claude/research/scalp/.trade_cache/<COIN>.jsonl` because the
Hyperliquid `recentTrades` endpoint is hard-capped at 10 trades per call.
Each /scalp run dedupes by `tid` and appends. Running `/loop 5m /scalp HYPE`
builds meaningful 5m/15m/1h coverage within 30 minutes. First call after
long idle = low coverage; say so.
