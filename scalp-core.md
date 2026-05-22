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
One line per row. ANY answer in the leak column = NO-TRADE, halt.
Applies identically to long and short entries.

| Row | Edge form (proceed)                | Leak form (KILL trade)                |
|-----|------------------------------------|---------------------------------------|
| Timing      | Thesis pre-dates the move          | Reactive — looking because it ran     |
| Levels      | From Saturday plan / pre-defined   | Drawn now on the rip                  |
| Trigger src | Macro / structural / OSINT         | X scroll, group-chat, sentiment spike |
| Conviction  | Scenario-weighted A/B/C            | Single-outcome "this is the one"      |
| State       | Calm, planned                      | Heart-rate up, urge to size up        |
| Invalidation| Pre-defined, written               | "I'll know when I see it"             |

Out-of-plan flag: if thesis is NOT in this week's Saturday plan but row 1 still
clears (pre-dates the move via a fresh structural catalyst), tag `OOP-1`.
Cap is 1 OOP/week — refuse a second OOP entry the same week.

Print one line: `BEHAVIORAL: CLEAR (cooldown clear, R16 6/6 edge)` or the
halt line.

## Step 1 — Fetch data (deterministic, never inline-curl)
Run: `python3 /Users/nyanyk/Claude/research/scalp/fetch_market.py <COIN> [--deep]`
Use ONLY the returned block. Never re-derive timestamps. If output is a
`DATA UNAVAILABLE` line, report it and STOP — no estimated numbers.

The output includes `taker_delta` — REAL aggressor flow from a local trade
cache that grows across repeated /scalp calls. Use `delta_usdc` and
`buy_share_pct` per window for buyer-vs-seller pressure. CHECK `coverage_pct` —
if <50% the window is partial and the signal is weak; report that explicitly.

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

### 6b. Sizing math (required in every trigger block)
For each trigger output the math, do not skip:
```
account_equity = $<E>        risk_cap = <%>
$risk = E × risk_cap         stop_distance = |entry − stop|
position_size_coins = $risk / stop_distance
leverage_derived = (position_size_coins × entry) / E
```
Leverage is the OUTPUT, never an INPUT. Submit hard SL to Hyperliquid at
entry — no mental stops, no widening (tightening is fine). The formula is
direction-neutral: for a short, stop is above entry; `|entry − stop|` is
unchanged.

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

## Step M — MANAGE mode (when already in a position)

Read entry price and direction from conversation (or args). Compute:
- Unrealized P&L: long = (mark − entry) / entry; short = (entry − mark) / entry
- Tape state: classify from taker_delta + recent candle bodies +
  vol_zscore. One of {impulse, distribution, chop, reversal-up,
  reversal-down}. Distribution = high volume + small body + close in
  bottom third of range on recent 15m/1h.
- Stop suggestion: tighten to lock minimum 1R if price has run > 2R
  from entry. Long: trail below the most recent 15m swing low that held with
  reclaim. Short: trail above the most recent 15m swing high that held with
  rejection.
- Scale ladder: 50% at T1, 30% at T2, 20% trail. T1 is the next
  structural mean-revert level; T2 is the structural target; trail is the
  lottery ticket. For a short, T1/T2 are levels DOWN.
- Add decision: apply Step 6 add rule. Default = NO.

### MANAGE output format

```
POSITION: <COIN> <long|short> from <entry> | now <px> | unrealized <+/-%>
COOLDOWN: <none|active until <T>> (does not block MANAGE, but blocks new entries)
TAPE: <impulse|distribution|chop|reversal-up|reversal-down> — <one clause why,
  cite taker_delta or candle evidence>
MACRO: <CLEAR|VETO ...> (<trajectory if known>)
STOP: current <x>  →  move to <y> (locks <+%>)
SCALE LADDER:
  T1 <px> (<+%>, R:R <r>): take 50% off
  T2 <px> (<+%>, R:R <r>): take 30% off
  Trail 20% with stop at <px>
ADD: <NO — why> | <YES at <px> with <% size>, stop <px>>
INVALIDATION: <one line — what kills the runner>
COUNTER-CASE: <one clause — what could flip this in next 1h>
ACTION NOW: <one decisive sentence>

JOURNAL STUB (paste on close):
  date: <UTC date>  asset: <COIN>  side: <long|short>
  entry: <px>  exit: __  size: <%risk / coins>
  outcome: __R   exit-reason: <T1|T2|stop|tape-flip|time-stop|manual>
  loss>0.7R? <yes|no> (if yes → 24h cooldown starts now)
  lesson: __
```

## Output — ENTRY QUICK (default for ENTRY mode)

The direction module supplies: VERDICT vocabulary, the counter-case line
label (Bear case for long / Bull case for short), and the trigger labels.
Fill the `<...>` slots accordingly.

```
SCALP — <COIN> <LONG|SHORT> | <sgt> / <utc> | US open in Xh (or "US session live, close in Xh")
BEHAVIORAL: <CLEAR (cooldown ok, R16 N/6 edge)|HALT reason>
VERDICT: <direction verdict vocab>  MACRO: <CLEAR|VETO ...>  Conviction: <low|med|high>
Risk cap: <0.5%|1%|2%>  (flag if default)
WEEKEND: <size x0.5 if weekend_window and short, else omit line>
Range <floor> – <ceiling> | now <mid> (<pos in range>)
Taker flow: 5m delta <±$Xk> (<buy_share>%)  15m <±$Xk>  [coverage <%>]
Triggers:
  A <name>: entry <px> / stop <px> / T1 <px> T2 <px> (R:R T1 <r>, T2 <r>)
    SIZE: equity $<E> × <%> = $<risk> ÷ <stop_dist> = <coins>  lev <Nx>
  B <name>: entry <px> / stop <px> / T1 <px> T2 <px> (R:R T1 <r>, T2 <r>)
    SIZE: equity $<E> × <%> = $<risk> ÷ <stop_dist> = <coins>  lev <Nx>
  [C <name>: ... — short module only]
Invalidation: <one line>
<Bear case|Bull case>: <one clause — what kills this in next 1h>
Next decision bar: <e.g. 14:00 UTC 1h close — confirms by closing > X>

JOURNAL STUB (paste on close):
  date: <UTC date>  asset: <COIN>  side: <long|short>
  entry: <px>  stop: <px>  size: <%risk / coins>  setup: <trigger name>
  thesis: <one line>
  R16: <which rows = edge>   plan-status: <in-plan|OOP-1>
  outcome: __R   exit-reason: __   lesson: __
```

## Output — ENTRY DEEP (`/scalp deep`)
Append: BTC + BTC.D regime breakdown; positioning analysis
(funding/premium/OI/taker_delta full window); multi-TF structure (incl 4h/1d);
session detail (handoff/econ/weekend); full risk section; upgrade-to-trend-trade
condition. (ATH/discovery state already covered in Step 3 of the direction module.)

## /loop usage (hands-off monitoring)
`/loop 5m /scalp HYPE` (or `/loop 5m /scalp short HYPE`) — runs QUICK every 5
min, also FEEDS THE TRADE CACHE. After ~6 loops (30 min) taker_delta coverage
will be meaningful. Stay SILENT unless: verdict becomes actionable
(LONG-NOW / SHORT-NOW), a trigger fires, VETOED flips, OR (in MANAGE mode)
STOP/scale level hits or TAPE flips. Ping on state change only, never every tick.
