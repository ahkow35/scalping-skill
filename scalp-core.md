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

Two checks. If either fails: output `NO-TRADE — BEHAVIORAL HALT` with the
failed item, and STOP. Do not fetch data, do not run macro.

### 0a. Cooldown check (ENTRY + MANAGE)
Read from earlier conversation, or ask once:
- "Was the last closed trade a loss > 0.7R?" → 24h cooldown. NO-TRADE on new
  entries until window clears. (MANAGE allowed: existing position only.)
- "Have the last 2 entries been FOMO-form (any leak row in 0b below)?" →
  3-day cooldown. NO-TRADE.
If user says "no record / fresh slate," accept and proceed.

### 0b. R16 vibe check (ENTRY only — skip in MANAGE)
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
- ≤3/6 edge → conviction capped at **low**; name all leak rows; strongly consider sitting out
- Any leak in Timing or State → add one explicit sentence in output: "BEHAVIORAL CAUTION: <row> leaked — size accordingly"

Out-of-plan flag: if thesis is NOT in this week's Saturday plan but row 1 still
clears (pre-dates the move via a fresh structural catalyst), tag `OOP-1`.
Cap is 1 OOP/week — refuse a second OOP entry the same week.

Printing rule (silence-by-default):
- CLEAR (cooldown ok, R16 6/6) → **omit the BEHAVIORAL line entirely**. The
  default state does not need to be repeated every run.
- Any leak → print `BEHAVIORAL: <N>/6 edge — leaks: <row names> — conviction
  capped at <low|med>` and apply the conviction cap.
- Halt → print `BEHAVIORAL: HALT (<reason>)` and stop (no data fetch).
- DEEP mode → always print the full 6/6 score even when clear, so the
  discipline check is visible in the detailed report.

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

The output includes `session.weekend_window` — true Fri 20:00 → Sun 20:00 UTC.
The short module uses it; the long module ignores it.

After Step 1, hand off to the direction module: Step 2 (macro veto),
Step 3 (structure), Step 4 (triggers). Then return here for Steps 5–6.

## Step 5 — Session overlay (from session object)
Warn if setup straddles asia_handoff_soon. If us_session_live and within
~1h of us_open, widen stops / cut size (open volatility). Note econ window.
US open momentum window 13:30–15:30 UTC historically has 2-3x avg move
size — widen stops 30%, do NOT tighten.
Hard rule: scalps time-boxed; never carry into the Asia handoff. Exception:
confirmed continuation move in a strong regime may be trailed across handoff
with stop tightened to BE+.

## Step 6 — Risk discipline (HARD, always)

### 6a. Declared risk cap (mandatory input)
User must state active phase cap before triggers are output. Ask once if
unstated; default to **0.5%** and flag the default in output.
- **0.5%** — Phase 1 default.
- **1.0%** — Phase 1 A+ setup, pre-approved in Saturday plan only.
- **2.0%** — Phase 2 cap (only if Phase 2 is active).

### 6b. Sizing math (required in every trigger block — one inline)
Single line per trigger, all values explicit:
```
SIZE: $<E> × <C>% = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
```
Leverage is the OUTPUT (coins × entry / E), never an INPUT. Submit hard SL
to Hyperliquid at entry — no mental stops, no widening (tightening is fine).
The formula is direction-neutral: for a short, stop is above entry;
`|entry − stop|` is unchanged.

### 6c. Discipline rules
- Structural stop only — never noise-tight.
- If stop distance × required size > risk cap -> CUT SIZE, never tighten.
- Skip any setup with R:R < 2:1 on T1 AND R:R-weighted-across-scales < 2.5:1.
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
    "A": {"entry": 73.55, "stop": 72.30, "t1": 75.0, "t2": 75.83, "rr_t1": 1.16, "rr_t2": 1.82},
    "B": null
  },
  "trigger_used": null
}
JSON
```

The command prints a `trade_id` to stdout. Capture it. Print it in the
JOURNAL STUB at the top, replacing the freeform date line:

```
JOURNAL STUB:
  trade_id: <returned_id>
  ...
```

For action verdicts (`LONG-NOW`/`SHORT-NOW`) set `trigger_used: "A"` (or B/C).
For non-action verdicts set `trigger_used: null`.

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
POSITION: <COIN> <long|short> from <entry> | now <px> | unrealized <±%>
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
- **Action verdict** (`LONG-NOW` / `SHORT-NOW`) → full block, JOURNAL STUB included.
- **No-action verdict** (`WAIT` / `NO-TRADE` / `VETOED` / `HALT`) → compact
  block, JOURNAL STUB **suppressed** (no trade firing = nothing to journal yet).

Rules that apply to BOTH shapes:
- Omit the `BEHAVIORAL:` line when clear (see Step 0 printing rule).
- Omit the `WEEKEND:` line unless `session.weekend_window=true` AND direction=short.
- Omit the `Risk cap:` line when it's the 0.5% default; flag it only when 1% / 2%
  is in play. Otherwise include it on the VERDICT line as `Risk: <X%>`.
- Time header: `<sgt> | <utc>`. Append the US-session clause **only** when within
  1h of `us_open` or `us_close` (decision-relevant); omit otherwise.

### QUICK — action verdict (LONG-NOW / SHORT-NOW)

```
SCALP — <COIN> <LONG|SHORT> | <sgt> | <utc>  [US open in Xh | US close in Xh]
[BEHAVIORAL: <leak line> — omit if clear]
VERDICT: <V>  MACRO: <CLEAR|VETO ...>  Conviction: <low|med|high>  [Risk: <X%> if non-default]
[WEEKEND: size x0.5 — short + weekend only]
Range <floor> – <ceiling> | now <mid> (<pos>)
Flow: 5m <±$Xk> (<buy_share>%)  15m <±$Xk>  [cov <%>]
Book: micro <px> vs mid <px> (dev <±X> bps)  spread <Y> bps
Triggers:
  A <name>: <entry> / SL <stop> / T1 <px> T2 <px> (RR <r1>/<r2>)
    SIZE: $<E> × <C>% = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
  B <name>: <entry> / SL <stop> / T1 <px> T2 <px> (RR <r1>/<r2>)
    SIZE: $<E> × <C>% = $<risk> ÷ $<stop_dist> = <coins> <COIN> @ <Nx> lev
  [C <name>: ... — short module only]
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
Watching: A <name> at <entry> | B <name> at <entry>  [C ... — short only]
Reason: <one clause — why not now, cite the missing condition>
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
<COIN> <V> | <conv> | <floor>↔<ceil> @ <mid> | dev <±X>bps | next <when>
```
Examples:
- `HYPE WAIT | med | 72.5↔73.4 @ 72.9 | dev −0.2bps | next 14:00 UTC`
- `HYPE LONG-NOW | high | A 72.55 SL 72.20 RR 1.6 | size 28.6 @ 1.0x | dev +4bps`
  (when an entry is firing, swap the range field for the firing trigger detail)
- `HYPE VETOED | n/a | reason: BTC −2.1% on rising vol | next 14:00`

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
- On HALT: `<COIN> HALT | <reason>` — single line, no other fields.
- On data-unavailable: `<COIN> DATA-UNAVAILABLE | <source>` and stop.
- State-change discipline: in `/loop` TINY, **suppress output entirely** when
  the verdict, TAPE state, MACRO veto, conviction tier, and STOP/T1/T2/ADD
  decisions are all unchanged from the previous TINY line. Only emit on
  state change. (One exception: emit at most every 30 minutes anyway as a
  liveness heartbeat.)

## /loop usage (hands-off monitoring)
**Default to TINY in `/loop`.** Examples:
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
(`LONG-NOW` / `SHORT-NOW`) or any MANAGE event requiring a decision
(STOP move, T1/T2 hit, TAPE flip, invalidation triggered). One QUICK on
the transition, then back to TINY for the steady state.
