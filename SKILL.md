---
name: scalp
description: >
  Live tactical quant scalp thesis and position management for any Hyperliquid
  perp (default HYPE), gated by a BTC + BTC-dominance macro veto, a behavioral
  preflight, real taker-aggressor delta, and UTC/SGT session awareness.
  Long AND short setups, each with its own protocol module. Three output sizes —
  TINY (one-line pulse, default for /loop), QUICK (standard), DEEP (full).
  Two modes — ENTRY (fresh thesis) and MANAGE (review open position).
  Plus three admin commands — `summary` (audit log aggregate stats),
  `resolve` (close out an open trade with outcome), `list-open` (list unresolved
  entries). Trigger phrases: "/scalp", "/scalp <COIN>", "/scalp tiny <COIN>",
  "/scalp deep <COIN>", "/scalp short <COIN>", "/scalp tiny short <COIN>",
  "/scalp deep short <COIN>", "/scalp manage [short] <COIN> <entry>",
  "/scalp tiny manage [short] <COIN> <entry>", "/scalp summary [--since-days N]",
  "/scalp resolve <trade_id> <R> <exit_reason> [lesson]", "/scalp list-open",
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

### `/scalp summary [--since-days N]`

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

## Step 1 — Determine direction, then load the protocol

- Long (default): args do NOT contain `short` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-long.md`.
- Short: args contain `short` → load BOTH
  `/Users/nyanyk/Claude/research/scalp/scalp-core.md` and
  `/Users/nyanyk/Claude/research/scalp/scalp-short.md`.

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
- `/loop` invocations default to TINY unless QUICK/DEEP is explicit.
- Coin arg defaults to HYPE.
- **Step 0 Behavioral preflight runs FIRST** (cooldown + R16 vibe check).
  Cooldown fail → NO-TRADE — BEHAVIORAL HALT, do not fetch data.
  R16 leaks → conviction penalty + named warning; trade proceeds.
- Macro veto runs SECOND, from the direction module. Includes hard
  NO-TRADE on FOMC/CPI/NFP/PCE days + extreme funding (long: >+0.05%/8h;
  short: <-0.05%/8h).
- User must declare active risk cap (0.5% / 1% / 2%) before triggers;
  default 0.5% and flag it.
- Every trigger block shows the sizing math (equity × cap = $risk;
  $risk ÷ stop_distance = position size; leverage derived, not chosen).
- Output must include a JOURNAL STUB block (ENTRY + MANAGE).
- ALWAYS check `taker_delta.coverage_pct` — if <50%, call out the
  partial coverage and weight the signal accordingly.
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
