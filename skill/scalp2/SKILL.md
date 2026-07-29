---
name: scalp2
description: >
  Deterministic scan2 scanner + judgment/discipline layer for Hyperliquid perp
  scalps — a mechanical universe scan (card2.py) that emits ranked setup
  cards, reviewed by an LLM judgment layer that may only VETO or DOWNGRADE,
  never upgrade, never widen a stop, never add a suppressed setup. Plus an
  audit-log admin passthrough (summary/resolve/list-open/replay), scoped to
  scan2-originated entries via `--system scan2`. Trigger phrases: "/scalp2",
  "/scalp2 <COIN>", "/scalp2 manage <COIN> <entry> [short]", "/scalp2 summary".
---

# scalp2

Activating the scalp2 scanner + judgment companion. Full protocol lives in
`/Users/nyanyk/Claude/research/scalp/scalp2.md` — read it before applying
judgment to any card.

## Step 0 — Admin passthrough (short-circuit)

If the first arg is `summary`, `resolve`, `list-open`, or `replay`, run the
matching `audit_log.py` / `replay.py` command from
`/Users/nyanyk/Claude/research/scalp/` (same CLI as v1 `/scalp`), scoped to
scan2 entries:

```bash
python3 /Users/nyanyk/Claude/research/scalp/audit_log.py summary --system scan2
```

`resolve <trade_id> <R> <exit_reason> [lesson]` and `list-open` / `replay`
take the same arguments as v1 — no scan2-specific flags.

## Step 1 — Scan (`/scalp2` or `/scalp2 <COIN>`)

Run from `/Users/nyanyk/Claude/research/scalp/`:

```bash
python3 scan2.py --session manual --window-sec 120 [--coins <COIN>]
```

Print the script's output verbatim first. Then apply the judgment layer
described in `scalp2.md`: read each card's `lean`, structural entry/stop/
targets, flow line, and flags, and decide VETO or DOWNGRADE only — never
upgrade a card, never widen a stop, never add a setup the script suppressed.

## Step 2 — Manage (`/scalp2 manage <COIN> <entry> [short]`)

Follow the manage procedure in `scalp2.md` exactly: re-run the scan scoped
to that coin, report thesis intact/degraded/invalidated in ≤10 lines, and
never propose adds — exit now if the invalidation condition is met.

## Step 3 — Logging a taken card (REQUIRED, not optional)

If the human takes a card, log it immediately on fill — never upgrade,
downgrade, or add to the trigger values printed on the card:

```bash
python3 scan2.py log-entry <COIN> <long|short> <entry> <stop> <t1> <t2>
```

On exit, resolve the printed `trade_id`:

```bash
python3 audit_log.py resolve <trade_id> <R> <exit_reason> [lesson]
```

A card that's never logged never feeds `session_stop_active` or the
40-trade evidence gate (`scalp2.md`) — both read only `system=scan2,
mode=ENTRY` rows.

## Hard rules (see `scalp2.md` for full detail)
- Entry only on a retest inside the zone — never chase.
- Stops are structural: cut size to manage risk, never tighten/widen them.
- Net R:R must clear the 2.0 floor before a card is shown.
- Hard time-stop at +60 minutes.
- Session stop: 2 losses in a 12h window halts new cards.
