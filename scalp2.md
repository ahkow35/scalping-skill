# scalp2 protocol — scanner + discipline layer

Deterministic scan: `python3 scan2.py` (cron: `--session us`, manual:
`--session manual --window-sec 120`). The LLM/judgment layer applies ONLY
on manual invocation and in manage mode; it may VETO or DOWNGRADE a card,
never upgrade, never widen a stop, never add a setup the script suppressed.

## Reading a card
- lean = mechanical label (OI/price window read + sweep flags). It is NOT
  a signal. You decide.
- Entry: limit order inside the retest zone only. Card dead if price never
  retests. Never chase beyond the zone.
- Stop/T1/T2 are structural. If filled: stop to breakeven at T1 (50% out),
  remainder to T2. Hard time-stop: exit review at +60 min.
- flow line LOW-SAMPLE = REST sampling below 50% completeness; treat flow
  as unknown, not as confirmation.

## Manage mode — `/scalp2 manage <COIN> <entry> [short] [HH:MM-utc]`
1. `python3 scan2.py --session manual --window-sec 120 --coins <COIN>
   --no-notify`
2. Report ≤10 lines: thesis intact/degraded/invalidated; distance to
   stop/T1/T2; minutes since entry vs 60-min time-stop; any new veto
   (funding flip beyond ±0.03%/8h, BTC 1h breakdown/breakout).
3. Never propose adds. If invalidation condition met: exit now.

## Taking a card REQUIRES logging it
A card that's never logged is invisible to `session_stop_active` and the
40-trade evidence gate — both read `system="scan2", mode="ENTRY"` audit rows,
and nothing else writes one. This is not optional bookkeeping; skipping it
silences the session stop and keeps risk stuck at the proving 0.5% forever.
- On fill: `python3 scan2.py log-entry <COIN> <long|short> <entry> <stop>
  <t1> <t2>` — immediately, before managing the position.
- On exit: `python3 audit_log.py resolve <trade_id> <R> <exit_reason>
  [lesson]` — the `trade_id` printed by `log-entry`.

## Admin (shared audit log, v2-filtered)
- `python3 audit_log.py summary --system scan2`
- resolve / list-open / replay: same CLI as v1.

## Risk constants (change only via spec update)
0.5%/attempt proving → 1.0% after ≥40 resolved v2 trades with positive
mean R. 10x leverage. 2-loss/12h session stop. Structural stops only —
cut size, never tighten.
