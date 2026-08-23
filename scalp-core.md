# Scalp — CORE protocol (direction-neutral)

Loaded for EVERY scalp invocation, paired with exactly one direction module:
- `/scalp [deep] <COIN>` → core + `scalp-long.md`
- `/scalp [deep] short <COIN>` → core + `scalp-short.md`

The direction module supplies Steps 2–4 and the output deltas (verdict
vocabulary, bear-vs-bull case line, weekend modifier). Everything else is here.

The skill runs in one of TWO modes:
- **ENTRY mode** (default): user asking for a fresh thesis. Steps 0–6, QUICK/DEEP output.
- **MANAGE mode**: user is already in a position. Step M output.

Detect MANAGE mode if user says any of: "review my long", "review my short",
"manage my position", "/scalp manage <COIN> <entry>", "i'm in at <price>",
"what's the move now" while referencing an open position from earlier in the
conversation. Otherwise ENTRY. In MANAGE mode the direction is taken from the
stated position (or args), and the matching direction module is loaded.

## Step 0 — Behavioral preflight (RUN FIRST, before any data fetch)

Five checks:
- **Daily stop (0a)**: HARD ENTRY gate. If active, emit `HALT (daily stop)`
  and do not fetch market data. MANAGE still proceeds.
- **Loss cooldown (0b)**: informational WARNING only — printed, but zero effect
  on verdict or conviction. Surfaces a 24h pause when the last resolved trade
  lost > 0.7R.
- **R16 vibe check (0c)**: leaks cap conviction and are flagged. At low
  conviction the entry fires at PROBE size (25%) instead of full.
- **Coin lockout (0d)**: HARD ENTRY gate per coin+side. Two material losses
  (≤ −0.5R) on the same coin+side within 12h lock that coin+side for 24h.
- **Tilt-coin guard (0e)**: static conviction cap on coins with a documented
  account-history tilt (currently DOGE).
(The macro veto in the direction module is separate and DOES still hard-stop.)

### 0a. Behavioral state + daily stop (ENTRY + MANAGE)
Read state from the audit log — works under /loop with no conversation:
```bash
python3 /Users/nyanyk/Claude/research/scalp/behavioral.py
```
Returns `daily_stop {active, until_utc, realized_r_24h, reason}`,
`cooldown {active, until_utc, reasons}`, `oop_this_week`.
- `daily_stop.active` true in ENTRY mode → HARD `HALT (daily stop)`.
  Do not fetch market data, do not produce an action verdict, and do not let
  R16/flow/macro override it. Print:
  `BEHAVIORAL HALT: daily stop active (<reason>) until <until_utc> —
  resolve stale open action entries if this looks wrong`.
  Then emit the no-action HALT shape and audit-log it with
  `behavioral.daily_stop` in the payload. This gate is keyed off
  `resolved_at_ms`, so stale unresolved action entries can blunt it.
- `daily_stop.active` true in MANAGE mode → print the same warning but PROCEED.
  Managing an open position is never blocked.
- `cooldown.active` true → 24h loss cooldown (last resolved trade lost >0.7R).
  Print a prominent `BEHAVIORAL WARNING: cooldown active (<reason>) —
  informational only` line and PROCEED with the full analysis. The cooldown
  does NOT block the entry and does NOT affect the verdict or conviction —
  it is a heads-up that the last trade was a material loss, nothing more.
- If the log is empty / fresh slate, the script returns all-clear — omit the
  warning entirely.

Empirical basis (2025–26 account post-mortem, 11,944 fills): a daily-loss
circuit breaker was net-positive on every one of the 16 days it would have
tripped — losses avoided ~$425k, later-same-day gains forfeited $0. The daily
stop is the single highest-value rule in this skill; never soften it.

### 0d. Coin lockout (ENTRY only — HARD gate per coin+side)
`behavioral.py` returns `coin_lockout {active, locked: [{coin, side,
until_utc, losses_12h}]}` — two material losses (≤ −0.5R) resolved on the
same coin+side within 12h lock that coin+side for 24h from the second loss.
- ENTRY request matching a locked coin+side → HARD halt. Do not fetch market
  data. Print `BEHAVIORAL HALT: <COIN> <side> locked out (<n> losses in 12h)
  until <until_utc> — the re-entry is the tilt, not the setup` and emit the
  no-action HALT shape (TINY: `<COIN> HALT | coin lockout (until <T>)`).
  Audit-log it with `behavioral.coin_lockout` in the payload.
- Other coins and the opposite side are unaffected. MANAGE always proceeds.
- Empirical basis (post-mortem): repeated re-entry into the same losing
  coin+side accounted for ~$345k of realized losses (DOGE long −$219k,
  ETH short −$107k) that this lockout would have blocked.

### 0e. Tilt-coin guard (ENTRY only — static list)
Coins with a documented account-history tilt get a standing conviction cap:
entries are capped at **med** (never NOW/full size) and are never valid as
OOP entries — in-plan only. Current list:
- **DOGE** — lifetime realized −$228.5k (2025 post-mortem: an October
  liquidation followed by five weeks of $50k re-entries into the same falling
  long). The cap stands until 20 resolved DOGE trades show positive expectancy.
Print `TILT-COIN: <COIN> — conviction capped at med (see post-mortem)` when it
binds. This list is maintained by hand; update it from post-mortem evidence,
not vibes.

### 0c. R16 vibe check (ENTRY only — skip in MANAGE)
One line per row. Leaks reduce conviction and are flagged — they do NOT halt the trade.
Applies identically to long and short entries.

| Row | Edge form (proceed)                | Leak form (flag + conviction penalty) |
|-----|------------------------------------|---------------------------------------|
| Timing      | Thesis pre-dates the move          | Reactive — looking because it ran     |
| Levels      | From Saturday plan / pre-defined   | Drawn now on the rip                  |
| Trigger src | Macro / structural / OSINT         | X scroll, group-chat, sentiment spike |
| Conviction  | Scenario-weighted A/B/C            | Single-outcome "this is the one"      |
| State       | Calm, planned                      | Heart-rate up, urge to size up        |
| Invalidation| Pre-defined, written               | "I'll know when I see it"             |

Scoring:
- 6/6 edge → conviction = high (no penalty)
- 4–5/6 edge → conviction capped at **med**; name the leak rows
- ≤3/6 edge → conviction capped at **low**; name all leak rows; fire at PROBE size (25% of cap)
- Any leak in Timing or State → add one explicit sentence in output: "BEHAVIORAL CAUTION: <row> leaked — size accordingly"

Out-of-plan flag: if thesis is NOT in this week's Saturday plan but row 1 still
clears (pre-dates the move via a fresh structural catalyst), tag `OOP-1`.
Cap is 1 OOP/week — if `oop_this_week` (from behavioral.py) is already >= 1,
refuse a second OOP entry this week. Log `plan_status: "OOP-1"` so the count
holds across /loop runs.

Printing rule (silence-by-default):
- CLEAR (daily stop clear, cooldown ok, R16 6/6) → **omit the BEHAVIORAL line entirely**. The
  default state does not need to be repeated every run.
- Daily stop active → print `BEHAVIORAL HALT: daily stop active (<reason>)
  until <until_utc> — resolve stale open action entries if this looks wrong`.
  ENTRY emits `HALT (daily stop)` and stops before market fetch; MANAGE proceeds.
- Any leak → print `BEHAVIORAL: <N>/6 edge — leaks: <row names> — conviction
  capped at <low|med>` and apply the conviction cap.
- Cooldown active → print `BEHAVIORAL WARNING: cooldown active (<reason>) —
  informational only` and PROCEED. No stop, no conviction effect. Stacks with
  a leak line if R16 also leaks.
- Coin lockout matching the requested coin+side → print the 0d HALT line;
  ENTRY stops before market fetch. A lockout on a DIFFERENT coin/side is not
  printed (silence-by-default).
- Tilt-coin entry (0e) → print `TILT-COIN: <COIN> — conviction capped at med
  (see post-mortem)` and apply the cap.
- DEEP mode → always print the full 6/6 score even when clear, so the
  discipline check is visible in the detailed report.
- Passive mode: also read `behavioral.passive_tilt`; if active, print
  `PASSIVE TILT: <n> losses — informational` and prefer SIT-OUT (Step 2 gate 4
  of scalp-passive.md).

## Step 1 — Fetch data (deterministic, never inline-curl)
Run: `python3 /Users/nyanyk/Claude/research/scalp/fetch_market.py <COIN> [--deep]`
Use ONLY the returned block. Never re-derive timestamps. If output is a
`DATA UNAVAILABLE` line, report it and STOP — no estimated numbers.

HIP-3 builder-deployed perps are supported via namespaced coin args
(`xyz:SPCX`, `vntl:SPACEX`, etc.). The fetcher routes ctx through the
deployer's dex; L2 / candles / trades use the namespaced coin directly.
BTC macro context is always pulled from core, regardless of primary's dex.
**Caveat**: macro veto and US-session overlay assume crypto-continuous
trading. For HIP-3 TradFi underlyings (stocks, FX, commodities) the
session timing logic is only meaningful for US-listed assets (NYSE hours
≈ US session window); for non-US underlyings, treat session overlay as
indicative and lean on instrument-specific event awareness instead.

The output includes `taker_delta` — REAL aggressor flow from a local trade
cache that grows across repeated /scalp calls. Use `delta_usdc` and
`buy_share_pct` per window for buyer-vs-seller pressure. CHECK `coverage_pct` —
if <50% the window is partial and the signal is weak; report that explicitly.

The output includes `book.execution` — Stoikov top-of-book microprice and the
instantaneous lean of the book. Key field: `microprice_dev_bps` (positive = bid
stack dominant, book leaning up; negative = ask stack dominant, leaning down).
Used by Step 6d (execution refinement) and Step M (tape state). `None` values
mean L2 was empty / malformed — callers fall through to original rules.

The output includes `regime` — a deterministic market-regime read (range
compression, BTC correlation, two-sided-flow score, directionality) with a
`regime_label` ∈ {trending, ranging, quiet, correlated-chop, unknown} and a
coarse `fade_ok` boolean. **Phase 1: READ-ONLY** — it does NOT change directional
verdicts yet. Surface it as the WEATHER line in the output. Its purpose is the
order-flow insight that directional edge is poor in `quiet` / `correlated-chop`
tape: when `regime_label` is `quiet` or `correlated-chop`, add one sentence to
the output noting directional edge is structurally low in this tape.

The output includes `vwap` — a deterministic UTC-day-anchored VWAP (1h
hlc3×volume), with `side` (mid above/below) and `dev_bps`. **Phase 1:
READ-ONLY** — it does NOT change verdicts or conviction. Surface it on the
Range line; the direction modules say how to read it (bias context + a
`fighting VWAP` flag). `None` = no volume yet today (just after UTC
midnight). Passive mode uses it as the deterministic mean.

The output includes `oi` — open-interest change over 1h/24h windows from a
local rolling cache (`.oi_cache.jsonl`; Hyperliquid only exposes current OI,
so the change signal builds across repeated /scalp calls, same as BTC.D).
Each window carries `oi_chg_pct`, `price_chg_pct` (window-aligned, from the
same cached sample) and a `read` ∈ {new-longs, short-covering, new-shorts,
long-unwind, flat, None}. **Phase 1: READ-ONLY** — context only; `read: None`
= cache warming for that window; say so instead of estimating.

The output includes `session.weekend_window` — true Fri 20:00 → Sun 20:00 UTC.
The short module uses it; the long module ignores it.

The output includes `flow` — a deterministic volume / order-flow read
(`flow.py`, mirrors `regime`). Fields: `coverage_ok`, `max_coverage_pct`,
`aggressor_bias` (buyers/sellers/balanced) + `avg_buy_share_pct`,
`volume_climax` ({ratio, direction} or null), `delta_divergence`
(bearish/bullish or null), `breakout_vol_ok`. It encodes Varma's "volume is
the key" heuristics — applied in Step 1b.

## Step 1b — Volume / flow confirmation gate (read `out['flow']`)

Varma's rule: volume is the signal that confirms whether a price move is real
([[samir-varma-react-to-risk-quant-trading]] §8a). The classifier states what
flow is doing; this gate maps it to conviction. **It only ever CUTS conviction
or sits you out — never raises it** (volume confirms an edge or it doesn't; it
never manufactures one). Apply AFTER the direction module sets the base verdict
and `side`; the final conviction = the LOWEST tier across the R16 check (0c),
this flow gate (1b), and the strip-BTC gate (1c). Tiers: high → med → low.

1. **Coverage — can you even see the flow?** `coverage_ok == false`
   (max_coverage_pct < 50) → you're scalping half-blind: cap conviction at
   **low** and flag `⚠ low coverage`. Re-run /scalp every ~5m to build the
   trade cache before sizing up.
2. **Aggressor bias must agree with `side`.** long wants `buyers`, short wants
   `sellers`.
   - `balanced` → no confirmation: cut **one** tier.
   - OPPOSES the side (long into `sellers` / short into `buyers`) → cut **two**
     tiers (→ low) + flag `flow opposes`; strongly prefer WAIT.
3. **Delta divergence = exhaustion, don't chase.** long + `bearish` (price up
   on selling) → cut one tier and do NOT take a fresh breakout long into it;
   short + `bullish` (price down on buying) → cut one tier.
4. **Volume climax** (`volume_climax.direction`): `down` = capitulation
   (supports longs / covering shorts, warns against a fresh short); `up` =
   blow-off (supports shorts / taking profit, warns against a fresh long). A
   climax IN your entry direction = you're late → cut one tier. A climax
   AGAINST the prior move (reversal in your favour) is a fade signal, not a cut.
5. **Breakout entries need volume.** If the fired trigger is a breakout /
   momentum entry and `breakout_vol_ok == false`, the breakout is unconfirmed →
   cut one tier (a breakout on thin volume is suspect).

Cuts stack (take the minimum tier). If the gate drives the final conviction to
low AND macro/regime is also weak, prefer **PROBE** over WAIT; only go to WAIT
if no trigger fires at all. Surface the result on the FLOW line of the output.
If `flow` is a string (DATA UNAVAILABLE) or all fields null, treat as
`coverage_ok == false`.

## Step 1c — Strip-BTC idiosyncrasy check (read `out['regime']` + BTC macro move)

A directional scalp should be paid for a **coin-specific** move, not for BTC
beta the whole market is riding. Borrowing the partial-correlation logic from
ML pair selection (filter the shared common factor before trusting the
relationship — Rotondi & Russo 2025), strip BTC and ask: is <COIN>'s move
idiosyncratic, or is the index just dragging it along? A move that is pure beta
carries no coin-specific edge. This is **distinct from the Step 2 macro veto**,
which hard-stops on BTC *danger* (a dump on rising vol); this gate cuts on BTC
*authorship* of the move even when macro is CLEAR (e.g. a calm BTC grind up
dragging the coin with it).

Inputs, all already fetched (no new data): `regime.btc_corr` (rolling BTC
correlation), the coin's own move (`regime.directionality` + recent candle
bodies / range position), and BTC's recent move (direction + rough magnitude)
from the fetched BTC macro candles — the same BTC move the macro veto reads.

**CUT-only** — like the flow gate, it can only lower conviction or sit you out,
never raise it. It consumes the numeric `btc_corr` input only; the
`regime_label` verdict stays READ-ONLY (Phase 1). Thresholds are PROVISIONAL —
revisit after 20 resolved trades.

1. **Pure BTC beta → cut one tier** (prefer WAIT if already low): `btc_corr ≥
   0.7` AND the coin's recent move is the SAME direction as BTC's AND the coin
   is NOT outrunning BTC (coin |move| ≲ 1.5× BTC |move|, i.e. negligible
   idiosyncratic residual). What you'd be trading is index beta, not a <COIN>
   setup. Flag `⚠ BTC-beta: <COIN> move is index-driven`. This operationalizes
   the `correlated-chop` weather into an actual conviction effect.
2. **Idiosyncratic residual → NO cut.** High corr but the coin is moving
   *against* BTC, or materially *beyond* it (coin |move| > 1.5× BTC |move|).
   That residual is the coin-specific edge you want — do not penalize it. State
   `strip-BTC: idiosyncratic residual — coin-specific move, no cut`.
3. **Already decoupled → NO cut.** `btc_corr < 0.4`: the move is not a BTC read.
   Note `strip-BTC: decoupled (corr <x>)`.
4. **Data unavailable** (`regime` null or `btc_corr` None): skip, say so, no cut.

Passive mode: informational only — passive fades the VWAP mean and does not
depend on idiosyncratic directional edge.

After Step 1/1b/1c, hand off to the direction module: Step 2 (macro veto),
Step 3 (structure), Step 4 (triggers) — or `scalp-passive.md` when `passive`
is in args. Then return here for Steps 4b–6. (The Step 1b flow-gate and Step 1c
strip-BTC conviction effects are applied once the direction module has set the
base verdict and side.)

## Step 4b — Conviction-to-size verdict mapping

Once triggers are defined (direction module Step 4) and the final conviction is
set (the LOWEST tier across the R16 check, the flow gate, and the strip-BTC
gate), map to the verdict and size:

| Final conviction | Verdict (long) | Verdict (short) | Size (% of cap) |
|---|---|---|---|
| high | LONG-NOW | SHORT-NOW | 100% |
| med | LONG-CLOSE | SHORT-CLOSE | 50% |
| low | LONG-PROBE | SHORT-PROBE | 25% |

All six are **action verdicts** — they produce a JOURNAL STUB and are fired live.
Only go to WAIT when no trigger fires. If the macro veto blocks, the verdict is
VETOED / NO-TRADE regardless of conviction.

The conviction tier shown on the VERDICT line reflects the **final** conviction
after all cuts. The verdict name tells the user what size to use; the sizing
math in Step 6b applies the verdict's multiplier.

## Step 5 — Session overlay (from session object)
Warn if setup straddles asia_handoff_soon. If us_session_live and within
~1h of us_open, widen stops / cut size (open volatility). Note econ window.
US open momentum window 13:30–15:30 UTC historically has 2-3x avg move
size — widen stops 30%, do NOT tighten.
Hard rule: scalps time-boxed; never carry into the Asia handoff. Exception:
confirmed continuation move in a strong regime may be trailed across handoff
with stop tightened to BE+.

## Step 6 — Risk discipline (HARD, always)

### 6a. Declared risk cap + equity (mandatory input)
Read both from the profile — required for the sizing math, and the only way
/loop can size without asking:
```bash
python3 /Users/nyanyk/Claude/research/scalp/profile.py get
```
Returns `{equity, phase}`. Default cap follows phase:
- **0.5%** — Phase 1 default.
- **1.0%** — Phase 1 A+ setup, pre-approved in Saturday plan only (per-trade
  override, declared inline — not stored).
- **2.0%** — Phase 2 cap (only if `phase` == 2).

The verdict tier applies a multiplier to the cap:
- NOW (high conviction): ×1.0 — full cap.
- CLOSE (med conviction): ×0.5 — half cap.
- PROBE (low conviction): ×0.25 — quarter cap.

If `equity` is null, ask once and offer to save:
`/scalp profile set equity <amount>`. Do NOT guess equity — without it, no
action verdict can print sizing. Flag the cap in output only when non-default.
Always state the verdict tier on the SIZE line so the multiplier is explicit.

### 6b. Sizing math (required in every trigger block — one inline)
Single line per trigger, all values explicit. SIZE line includes the verdict
tier multiplier:
```
SIZE (NOW): $<E> × <C>% = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
SIZE (CLOSE 50%): $<E> × <C>% × 0.5 = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
SIZE (PROBE 25%): $<E> × <C>% × 0.25 = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
```
Show only the line matching the verdict tier. Leverage is the OUTPUT (coins ×
entry / E), never an INPUT. Submit hard SL to Hyperliquid at entry — no mental
stops, no widening (tightening is fine). The formula is direction-neutral: for
a short, stop is above entry; `|entry − stop|` is unchanged.

### 6c. Discipline rules
- Structural stop only — never noise-tight.
- If stop distance × required size > risk cap -> CUT SIZE, never tighten.
- Skip any setup with NET R:R < 2:1 on T1 AND NET
  R:R-weighted-across-scales < 2.5:1. Net means gross R:R minus the shared
  `costs.py` round-trip cost model (`TAKER_FEE + SLIPPAGE_FRAC`, two fills)
  converted into R from the trigger's entry/stop distance.
- One line: "what invalidates this".
- **Add/pyramid rule**: adds require ALL of (a) better R:R than original
  entry, (b) smaller size than original (≤50%), (c) blended stop keeps
  existing position in profit, (d) not into a vertical impulse. Chasing the
  extreme of a ±20%/24h move (the high for a long, the low for a short) is
  not adding.

### 6d. Microprice execution gate (refines entry execution — does NOT veto)
Once a trigger fires, choose market vs limit using `book.execution`:
- **LONG entry**: market-take only if
  `microprice_dev_bps ≥ +(taker_fee_bps_reference + 1)` (book agrees with the
  long). Otherwise post limit at `min(trigger_entry, microprice − 1 bp)` and
  let price come to you.
- **SHORT entry**: symmetric — market-take only if
  `microprice_dev_bps ≤ −(taker_fee_bps_reference + 1)`. Otherwise post limit
  at `max(trigger_entry, microprice + 1 bp)`.
- **Scale-out (T1/T2 in MANAGE mode)**: same logic in reverse. Closing a long
  at T1 with `microprice_dev_bps` clearly negative = hit the bid now (book
  rolling against you). Clearly positive = post at ask. Symmetric for shorts.
- If `microprice` is `None` (empty / stale book) or `coverage_pct` of the L2
  feed is suspect, default to the trigger price as written. Never block a
  fired trigger on missing microprice.
- Rationale: Stoikov (2018). The book's instantaneous lean is the cheapest
  meaningful execution edge in the literature — refusing to pay the wrong side
  of fair value removes most adverse-selection on aggressive fills. This rule
  is execution-layer, not thesis-layer; it sharpens fills, it does not change
  what we trade.

### 6e. Spread / slippage / depth guard (PROVISIONAL thresholds)

Use `costs.execution_cost_r(entry, stop, spread_bps)` and `book.depth` when L2
is present. Missing/suspect book falls through to Step 6d's current fallback;
never block a fired trigger solely because book data is unavailable.

- If live execution `cost_r > 0.20R` → maker-only entry: post limit, never
  market-take. The setup can still fire, but do not pay taker into a cost-heavy
  book.
- If live execution `cost_r > 0.35R` → additionally downgrade one conviction
  tier and flag `⚠ cost-heavy`. This can turn NOW → CLOSE or CLOSE → PROBE.
- Depth cap: intended size must be ≤25% of visible top-3 depth on the entry
  side. Longs consume ask depth (`book.depth.ask_top3.sz`); shorts consume bid
  depth (`book.depth.bid_top3.sz`). If intended size is larger, cut size to the
  cap and flag `DEPTH: size capped to 25% top-3`.

These thresholds are provisional. Revisit after 20 resolved trades carry live
cost/depth data.

## Step 7 — Audit logging (HARD, always when emitting QUICK / DEEP / MANAGE-action-required)

Every QUICK, DEEP, and MANAGE-action-required output gets appended to the
audit log via the helper script. This is the foundation for honest
performance accountability — without it, the skill is unfalsifiable.

### When to log
- ENTRY QUICK with any verdict (LONG-NOW / SHORT-NOW / WAIT / NO-TRADE / VETOED / HALT): LOG.
- ENTRY DEEP: LOG (same payload shape as QUICK).
- MANAGE action-required (STOP move / T1/T2 hit / TAPE flip / ADD / invalidation): LOG.
- MANAGE no-action compact form: SKIP (state unchanged — not worth a row).
- TINY in `/loop`: SKIP. Too noisy; state-change-gated emits cover the audit need.
- TINY on state-change (heartbeat or one-shot `/scalp tiny`): SKIP. The next QUICK on transition logs.

### How to log

Right after printing the output block, shell out:

```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py log <<'JSON'
{
  "coin": "HYPE",
  "side": "long",
  "mode": "ENTRY",
  "verdict": "WAIT",
  "conviction": "med",
  "macro_status": "CLEAR",
  "macro_can_clear": true,
  "behavioral": {"cooldown": "clear", "r16_score": 6, "leaks": []},
  "inputs": {
    "mid": 71.94, "ath_state": "below_ath", "ath_dist_pct": -5.22,
    "btc_d": 55.93, "btc_d_24h_chg": null, "btc_d_coverage_h": 0.0,
    "btc_mark": 67383, "funding_8h": 0.00125, "premium": 0.00141,
    "weekend_window": false, "us_session_live": false,
    "taker_delta_5m": {"delta_usdc": 5152, "buy_share_pct": 94.0, "coverage_pct": 0.1}
  },
  "triggers": {
    "A": {"entry": 73.55, "stop": 72.30, "t1": 75.0, "t2": 75.83, "rr_t1": 1.16, "rr_t2": 1.82, "net_rr_t1": 1.08, "net_rr_t2": 1.74},
    "B": null
  },
  "trigger_used": null,
  "plan_status": "in-plan"
}
JSON
```

`plan_status` is `"in-plan"` or `"OOP-1"` (drives the weekly OOP cap; see
Step 0b).

The command prints a `trade_id` to stdout. **Capture it regardless of verdict
— every QUICK/DEEP output is logged.** But only ACTION verdicts
(`LONG-NOW`/`SHORT-NOW`) and MANAGE-action-required outputs DISPLAY a JOURNAL
STUB; print the trade_id at the top of that stub, replacing the freeform date
line:

```
JOURNAL STUB:
  trade_id: <returned_id>
  ...
```

For no-action verdicts (`WAIT`/`NO-TRADE`/`VETOED`/`HALT`) the row is still
logged with its trade_id, but NO stub is shown — there is no trade to journal
yet. Set `trigger_used: "A"` (or B/C) for action verdicts, `null` otherwise.

For MANAGE-action-required: payload uses `mode: "MANAGE"`, plus an `event`
field naming what triggered the action (`stop_move`, `t1_hit`, `t2_hit`,
`tape_flip`, `add_fired`, `invalidation`). The `triggers` block becomes
the current `scale_ladder` (T1/T2 prices, current SL).

### Resolving on close

When the trade closes (or the thesis is invalidated without an entry), run:

```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py resolve \
  <trade_id> <outcome_r> <exit_reason> "<lesson>"
```

`outcome_r` is the R-multiple (`-1.0`, `2.5`, etc.). `exit_reason` is one of
`T1`, `T2`, `stop`, `tape-flip`, `time-stop`, `manual`, `unfilled`. Lesson
is optional but recommended — that's the part that compounds.

### Reviewing performance

```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py summary [--since-days N]
```

Returns verdict distribution (catches calibration drift — if VETOED is 90%
of calls, the gate is mis-tuned) and per-setup expectancy across resolved
trades (catches "trigger A is actually net-negative at 2am" patterns).

```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py list-open
```

Shows entries not yet resolved with age in hours. Stale entries (>72h open
on an action verdict) usually mean the user forgot to resolve — prompt them.

### Failure handling

If `audit_log.py log` fails (disk full, permission error, etc.), report
the error to the user AND still emit the trading output — the audit log is
non-blocking. A missed audit row is better than a missed trade. But flag
the failure prominently so it gets fixed.

## Step M — MANAGE mode (when already in a position)

Read entry price and direction from conversation (or args). Compute:
- Unrealized P&L: long = (mark − entry) / entry; short = (entry − mark) / entry
- **Position age** (from the audit-log entry time, or ask once if unknown).
  Age > 24h on a scalp entry = the position is no longer a scalp — that is
  itself an ACTION-required event: the output must force an explicit decision
  (close, or state the new swing thesis + structural stop + reduced size),
  never a silent HOLD. Age > 72h with the position underwater → lead with
  `TIME-STOP: this is a stale losing hold, not a trade`. Empirical basis
  (post-mortem): all three 2025 blowups (−$384k across Aug 22 / Oct 10 /
  Dec 31) were scalp-sized theses held for days-to-weeks without a stop;
  none began as a large loss.
- Tape state: classify from taker_delta + recent candle bodies +
  vol_zscore. One of {impulse, distribution, chop, reversal-up,
  reversal-down}. Distribution = high volume + small body + close in
  bottom third of range on recent 15m/1h.
- Microprice lean (`book.execution.microprice_dev_bps`): persistent +dev across
  3+ consecutive /scalp snapshots = bid stack dominant = bullish supporting
  signal even if candles look flat; persistent −dev = bearish supporting
  signal. Use as a third leg alongside taker_delta and candles, not as a
  standalone tape classifier. Single-snapshot dev is noise.
- Stop suggestion: tighten to lock minimum 1R if price has run > 2R
  from entry. Long: trail below the most recent 15m swing low that held with
  reclaim. Short: trail above the most recent 15m swing high that held with
  rejection.
- Scale ladder: 50% at T1, 30% at T2, 20% trail. T1 is the next
  structural mean-revert level; T2 is the structural target; trail is the
  lottery ticket. For a short, T1/T2 are levels DOWN.
- Add decision: apply Step 6 add rule. Default = NO.

### MANAGE output format

**Two MANAGE shapes, selected by ACTION NOW:**
- **Action required** (STOP move / scale-out hit / TAPE flip / ADD fires /
  invalidation triggered) → full block + JOURNAL STUB.
- **No action** (TAPE state and all decisions unchanged from prior look) →
  compact one-block form, JOURNAL STUB **suppressed**.

Suppress the COOLDOWN line unless cooldown is active. Suppress COUNTER-CASE
in the compact form unless conviction is changing.

#### MANAGE — action required

```
POSITION: <COIN> <long|short> from <entry> | now <px> | unrealized <±%> | age <Xh>
[TIME-STOP: age <Xh> — no longer a scalp; decide: close or restate as swing with structural stop]
[COOLDOWN: active until <T> — blocks new entries, not MANAGE]
TAPE: <state> — <one clause, cite taker_delta / candles / microprice lean>
MACRO: <CLEAR|VETO ...>
STOP: <current> → <new> (locks <+%>)
SCALE LADDER:
  T1 <px> (<+%>, RR <r>): take 50%
  T2 <px> (<+%>, RR <r>): take 30%
  Trail 20% with stop at <px>
ADD: <NO — why> | <YES at <px>, <% size>, SL <px>>
INVALIDATION: <one line>
COUNTER-CASE: <one clause>
ACTION NOW: <one decisive sentence>

JOURNAL STUB (paste on close):
  date: <UTC date>  asset: <COIN>  side: <long|short>
  entry: <px>  exit: __  size: <%risk / coins>
  outcome: __R   exit-reason: <T1|T2|stop|tape-flip|time-stop|manual>
  loss>0.7R? <yes|no> (if yes → 24h cooldown starts now)
  lesson: __
```

#### MANAGE — no action (compact)

```
POSITION: <COIN> <long|short> @<entry> | now <px> <±%> | TAPE <state>
STOP: <current> (unchanged)  T1 <px>  T2 <px>
ACTION NOW: HOLD — <one clause why nothing changes>
Next: <when to re-check>
```

## Output — ENTRY QUICK (default for ENTRY mode)

The direction module supplies: VERDICT vocabulary, the counter-case line
label (Bear case for long / Bull case for short), and the trigger labels.
Fill the `<...>` slots accordingly.

**Two QUICK shapes, selected by verdict:**
- **Action verdict** (`LONG-NOW` / `LONG-CLOSE` / `LONG-PROBE` / `SHORT-NOW` /
  `SHORT-CLOSE` / `SHORT-PROBE`) → full block, JOURNAL STUB included.
- **No-action verdict** (`WAIT` / `NO-TRADE` / `VETOED` / `HALT`) → compact
  block, JOURNAL STUB **suppressed** (no trade firing = nothing to journal yet).

Rules that apply to BOTH shapes:
- Omit the `BEHAVIORAL:` line when clear (see Step 0 printing rule).
- Omit the `WEEKEND:` line unless `session.weekend_window=true` AND direction=short.
- Omit the `Risk cap:` line when it's the 0.5% default; flag it only when 1% / 2%
  is in play. Otherwise include it on the VERDICT line as `Risk: <X%>`.
- VWAP + OI are READ-ONLY context (Phase 1): they never change the verdict or
  conviction. Omit the VWAP segment when `vwap` is null; print the OI line as
  `OI: warming (<coverage_h>h)` while both window reads are null.
- Omit the `STRIP-BTC:` line unless the strip-BTC gate (Step 1c) actually cut
  conviction; the beta call is otherwise silent (an idiosyncratic/decoupled move
  is the default and needs no line).
- Time header: `<sgt> | <utc>`. Append the US-session clause **only** when within
  1h of `us_open` or `us_close` (decision-relevant); omit otherwise.

### QUICK — action verdict (NOW / CLOSE / PROBE)

```
SCALP — <COIN> <LONG|SHORT> | <sgt> | <utc>  [US open in Xh | US close in Xh]
[BEHAVIORAL: <leak line> — omit if clear]
VERDICT: <V>  MACRO: <CLEAR|VETO ...>  Conviction: <low|med|high>  [Risk: <X%> if non-default]
WEATHER: <regime_label> (compression <x> | BTC-corr <x> | 2-sided <x> | fade_ok <bool>)
[WEEKEND: size x0.5 — short + weekend only]
Range <floor> – <ceiling> | now <mid> (<pos>) | VWAP <px> (<above|below> <±X>bps)
Flow: 5m <±$Xk> (<buy_share>%)  15m <±$Xk>  [cov <%>]
FLOW-GATE: <bias> (<avg_buy_share>%) | cov <max>% | [climax <dir> ×<r>] [div <bearish|bullish>] [breakout-vol <ok|thin>] → conviction <unaffected | −1 | −2 | cap-low>
[STRIP-BTC: β-driven (corr <x>, move aligned w/ BTC) → conviction −1 — omit unless it cut]
OI: $<X>M | 1h <±%> → <read> | 24h <±%> → <read>   (or "warming <h>h" when read is null)
Book: micro <px> vs mid <px> (dev <±X> bps)  spread <Y> bps  depth top3 bid/ask <B>/<A> <COIN>
Triggers:
  A <name>: <entry> / SL <stop> / T1 <px> T2 <px> (RR <r1>/<r2>, net <n1>/<n2>)
    SIZE (<TIER <N>%>): $<E> × <C>% × <mult> = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
  [B <name>: <entry> / SL <stop> / T1 <px> T2 <px> (RR <r1>/<r2>, net <n1>/<n2>)
    SIZE (<TIER <N>%>): $<E> × <C>% × <mult> = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev]
Invalidation: <one line>
<Bear case|Bull case>: <one clause>
Next: <e.g. 14:00 UTC 1h close — confirms by closing > X>

JOURNAL STUB (paste on close):
  date: <UTC date>  asset: <COIN>  side: <long|short>
  entry: <px>  stop: <px>  size: <%risk / coins>  setup: <trigger name>
  thesis: <one line>
  R16: <edge rows>   plan-status: <in-plan|OOP-1>
  outcome: __R   exit-reason: __   lesson: __
```

### QUICK — no-action verdict (WAIT / NO-TRADE / VETOED / HALT)

Compact form. The user just needs the verdict, the reason, the levels we're
watching, and when to look again. Skip everything else.

```
SCALP — <COIN> <LONG|SHORT> | <sgt> | <utc>
[BEHAVIORAL: <leak line> — omit if clear]
VERDICT: <V>  MACRO: <CLEAR|VETO ...>  Conviction: <low|med|high>
WEATHER: <regime_label> (compression <x> | BTC-corr <x> | 2-sided <x> | fade_ok <bool>)
[FLOW: <bias> (<avg_buy_share>%) cov <max>% — include only when flow is the/a reason to wait]
Watching: A <name> at <entry> | B <name> at <entry>  [C ... — short only]
Reason: <one clause — why not now, cite the missing condition (e.g. flow opposes / low coverage / unconfirmed breakout / BTC-beta move)>
Next: <when to re-check — e.g. 14:00 UTC 1h close, or "on close above 73.4">
```

No JOURNAL STUB, no Range/Flow/Book lines, no full trigger sizing. If the
user wants depth on a WAIT, they can ask `/scalp deep`.

## Output — ENTRY DEEP (`/scalp deep`)
Append: BTC + BTC.D regime breakdown; positioning analysis
(funding/premium/OI/taker_delta full window); multi-TF structure (incl 4h/1d);
session detail (handoff/econ/weekend); full risk section; upgrade-to-trend-trade
condition. (ATH/discovery state already covered in Step 3 of the direction module.)
DEEP always prints the full BEHAVIORAL 6/6 score even when clear.

## Output — TINY (`/scalp tiny`, also default for `/loop`)

Single line, no JOURNAL, no preamble, no end-of-turn summary. Use for
high-frequency monitoring without flooding the screen.

**TINY — ENTRY mode** (no open position):
```
<COIN> <V> | <conv> | <floor>↔<ceil> @ <mid> | dev <±X>bps | wx <regime_label> | next <when>
```
Examples:
- `HYPE WAIT | med | 72.5↔73.4 @ 72.9 | dev −0.2bps | next 14:00 UTC`
- `HYPE LONG-NOW | high | A 72.55 SL 72.20 RR 1.6 | size 28.6 @ 1.0x | dev +4bps`
  (when an entry is firing, swap the range field for the firing trigger detail)
- `HYPE LONG-CLOSE | med | A 72.55 SL 72.20 RR 1.6 | size 14.3 @ 0.5x | dev +4bps`
  (CLOSE = 50% of cap, med conviction)
- `HYPE LONG-PROBE | low | A 69.50 SL 68.80 RR 2.1 | size 7.1 @ 0.25x | dev -2bps`
  (PROBE = 25% of cap, low conviction)
- `HYPE VETOED | n/a | reason: BTC −2.1% on rising vol | next 14:00`
- `HYPE HALT | daily stop -2.1R (until 2026-07-07 03:20 UTC) | resolve stale opens if wrong`

**TINY — MANAGE mode** (position open):
```
<COIN> <side> @<entry> | now <px> <±%> | TAPE <state> | <ACTION> | next <when>
```
Examples:
- `HYPE long @72.55 | now 72.91 +0.5% | TAPE chop | NO ACTION | next 14:00 UTC`
- `HYPE long @72.55 | now 73.42 +1.2% | TAPE impulse | MOVE SL to 72.80 | T1 73.10 hit, scale 50%`
- `HYPE short @74.20 | now 73.80 +0.5% | TAPE reversal-up | TRIM 30% NOW | invalidation 74.50`

Rules for TINY:
- Output the line and stop. No headers, no follow-up sentence, no "let me know".
- TINY may show gross `RR` only for brevity, but the decision to fire or wait
  still uses the NET R:R floor from Step 6c.
- Append ` | β-cut` to the TINY line when the strip-BTC gate (Step 1c) cut
  conviction (the move is index-driven). The lowered conviction tier is already
  shown; the token just names why.
- On daily-stop HALT: `<COIN> HALT | daily stop <±X.X>R (until <T>) | resolve stale opens if wrong`.
- On other HALT: `<COIN> HALT | <reason>` — single line, no other fields.
- On data-unavailable: `<COIN> DATA-UNAVAILABLE | <source>` and stop.
- State-change discipline: in `/loop` TINY, **suppress output entirely** when
  the verdict, TAPE state, MACRO veto, conviction tier, and STOP/T1/T2/ADD
  decisions are all unchanged from the previous TINY line. Only emit on
  state change. (One exception: emit at most every 30 minutes anyway as a
  liveness heartbeat.)

## /loop usage (hands-off monitoring)
**Default to TINY in `/loop`.** Run `/loop 5m /scalp <COIN>` continuously to
catch setups as they form — the audit log shows 1-12h gaps between entries
without it, which means setups are being missed. The trade cache also builds
meaningful taker-delta coverage (~30 min for 5m/15m windows) only under
sustained /loop. Examples:
- `/loop 5m /scalp HYPE` → TINY ENTRY every 5 min
- `/loop 5m /scalp short HYPE` → TINY ENTRY short every 5 min
- `/loop 5m /scalp manage HYPE 72.55` → TINY MANAGE every 5 min
- `/loop 5m /scalp quick HYPE` → explicit QUICK (only if user wants the full block)

TINY in `/loop` mode is **state-change gated**: emit nothing when verdict,
TAPE, MACRO veto, conviction, and STOP/scale decisions are all unchanged
from the prior line. Heartbeat: emit at least one line per 30 min regardless,
so the user knows the loop is alive. The trade cache fills on every run
whether or not output is emitted.

Escalate to QUICK automatically on state change to an action verdict
(`*-NOW` / `*-CLOSE` / `*-PROBE`) or any MANAGE event requiring a decision
(STOP move, T1/T2 hit, TAPE flip, invalidation triggered). One QUICK on
the transition, then back to TINY for the steady state.
