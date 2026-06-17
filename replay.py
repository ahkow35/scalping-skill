"""Counterfactual replay — score logged scalp decisions against what happened.

For every open ENTRY-mode audit row with trigger levels, fetch the candles
that followed it and mechanically simulate each trigger: did it fill, and did
the stop or the targets get hit first? Writes a `counterfactual` block back
onto the audit entry, which `audit_log.compute_summary` aggregates into
gate-value stats (WAIT missed-R, VETO avoided-R, per-setup expectancy).

Simulation conventions (deliberately simple, documented, conservative):
  - Fill = candle touch of the trigger entry price; filled AT entry, no
    slippage. Counterfactuals are therefore slightly optimistic on fills —
    this is gate-tuning evidence, not proof of live P&L.
  - Ladder per protocol: 50% out at T1, stop moves to breakeven; remaining
    50% out at T2 (the 20% trail is NOT simulated — T2 exit is the
    conservative stand-in). No T2 defined -> remainder scored at breakeven.
  - Same-candle conflicts (stop+target both inside one candle) drill down one
    interval level (15m -> 5m -> 1m) when a fetcher is provided; if still
    ambiguous, resolved CONSERVATIVELY (stop-first / breakeven-first) and
    flagged `ambiguous: true`.
  - Window: 72h after the logged decision (matches the stale-entry rule).
    Filled-but-unresolved at window end -> mark-to-market on the last close.
  - Real `outcome` (live trades) always wins; resolved entries are skipped.

Shorts are handled by mirroring prices (negation) into "long space" so a
single state machine serves both directions.

CLI:
  python3 replay.py [--window-h 72] [--force] [--id <trade_id>]
"""

import argparse
import json
import sys
import time

import audit_log
from fetch_market import _post_json, HL_INFO, iso_utc

SCAN_INTERVAL = "5m"
SCAN_INTERVAL_MS = 5 * 60_000
DRILL_INTERVAL = "1m"
MAX_CANDLES_PER_REQ = 400

NON_ACTION_VERDICTS = ("WAIT", "VETOED", "NO-TRADE", "HALT")


# ---------------------------------------------------------------- pure core

def trigger_r_levels(trigger, side):
    """Risk per unit and R-multiples of T1/T2 for a logged trigger."""
    entry = float(trigger["entry"])
    stop = float(trigger["stop"])
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError(f"degenerate trigger: entry {entry} stop {stop}")
    r_t1 = abs(float(trigger["t1"]) - entry) / risk
    t2 = trigger.get("t2")
    r_t2 = abs(float(t2) - entry) / risk if t2 is not None else None
    return {"risk": risk, "r_t1": r_t1, "r_t2": r_t2}


def _normalize(trigger, side, candles):
    """Mirror shorts into long space: negate prices (high/low swap)."""
    sign = 1.0 if side == "long" else -1.0
    trig = {
        "e": sign * float(trigger["entry"]),
        "s": sign * float(trigger["stop"]),
        "t1": sign * float(trigger["t1"]),
        "t2": sign * float(trigger["t2"]) if trigger.get("t2") is not None else None,
    }
    cds = []
    for c in candles:
        h, l = float(c["h"]), float(c["l"])
        cds.append({"t": int(c["t"]),
                    "h": max(sign * h, sign * l),
                    "l": min(sign * h, sign * l),
                    "c": sign * float(c["c"])})
    return trig, cds


def _step(phase, trig, lv, candle):
    """One candle through the state machine.

    Returns (next_phase, terminal, conflict):
      terminal = (status, r, ambiguous) ends the simulation;
      conflict = a string naming a same-candle ordering problem.
    """
    e, s, t1, t2 = trig["e"], trig["s"], trig["t1"], trig["t2"]
    h, l = candle["h"], candle["l"]

    if phase == "PENDING":
        if not (l <= e <= h):
            return phase, None, None
        stop_t, t1_t = l <= s, h >= t1
        if stop_t and t1_t:
            return phase, None, "fill_stop_t1"
        if stop_t:
            # resting order at entry fills on the way down to the stop
            return phase, ("stopped", -1.0, False), None
        if t1_t:
            return phase, None, "fill_t1"
        return "FILLED", None, None

    if phase == "FILLED":
        stop_t, t1_t = l <= s, h >= t1
        if stop_t and t1_t:
            return phase, None, "stop_t1"
        if stop_t:
            return phase, ("stopped", -1.0, False), None
        if t1_t:
            if t2 is None:
                return phase, ("t1_be", 0.5 * lv["r_t1"], False), None
            t2_t, be_t = h >= t2, l <= e
            if t2_t and be_t:
                return phase, None, "be_t2"
            if t2_t:
                return phase, ("t1_t2",
                               0.5 * lv["r_t1"] + 0.5 * lv["r_t2"], False), None
            if be_t:
                # T1 banked and entry touched in the same candle — order unknown
                return phase, None, "be_t2"
            return "RUNNER", None, None
        return phase, None, None

    # RUNNER: T1 banked, stop at breakeven, hunting T2 (t2 is not None here)
    be_t, t2_t = l <= e, h >= t2
    if be_t and t2_t:
        return phase, None, "be_t2"
    if be_t:
        return phase, ("t1_be", 0.5 * lv["r_t1"], False), None
    if t2_t:
        return phase, ("t1_t2", 0.5 * lv["r_t1"] + 0.5 * lv["r_t2"], False), None
    return phase, None, None


def _conservative(phase, conflict, lv):
    """Resolve a conflict without finer data. Returns (next_phase, terminal)."""
    if conflict in ("fill_stop_t1", "stop_t1"):
        return phase, ("stopped", -1.0, True)
    if conflict == "fill_t1":
        # T1 not banked; position continues as plain FILLED
        return "FILLED", None
    if conflict == "be_t2":
        return phase, ("t1_be", 0.5 * lv["r_t1"], True)
    raise ValueError(f"unknown conflict: {conflict}")


def simulate_trigger(trigger, side, candles, fetch_finer=None):
    """Simulate one trigger over candles (dicts with t/o/h/l/c, epoch-ms t).

    fetch_finer(start_ms, end_ms) -> finer candles for one coarse candle, or
    None. Used once per conflict; conflicts inside finer data resolve
    conservatively.
    """
    lv = trigger_r_levels(trigger, side)
    trig, cds = _normalize(trigger, side, candles)
    if not cds:
        return {"status": "unfilled", "r": None, "ambiguous": False}

    interval_ms = (cds[1]["t"] - cds[0]["t"]) if len(cds) > 1 else SCAN_INTERVAL_MS
    phase, ambiguous = "PENDING", False

    def finish(status, r, amb):
        return {"status": status,
                "r": round(r, 4) if r is not None else None,
                "ambiguous": amb}

    for cd in cds:
        phase, terminal, conflict = _step(phase, trig, lv, cd)
        if conflict and fetch_finer:
            finer_raw = fetch_finer(cd["t"], cd["t"] + interval_ms)
            if finer_raw:
                _, finer = _normalize(trigger, side, finer_raw)
                for fc in finer:
                    phase, terminal, fconflict = _step(phase, trig, lv, fc)
                    if fconflict:
                        phase, terminal = _conservative(phase, fconflict, lv)
                        ambiguous = True
                    if terminal:
                        break
                conflict = None
        if conflict:
            phase, terminal = _conservative(phase, conflict, lv)
            ambiguous = True
        if terminal:
            status, r, amb = terminal
            return finish(status, r, ambiguous or amb)

    # window exhausted
    last_c = cds[-1]["c"]
    if phase == "PENDING":
        return finish("unfilled", None, ambiguous)
    if phase == "FILLED":
        return finish("expired", (last_c - trig["e"]) / lv["risk"], ambiguous)
    # RUNNER — breakeven never hit, so last close >= entry in long space
    return finish("expired",
                  0.5 * lv["r_t1"] + 0.5 * (last_c - trig["e"]) / lv["risk"],
                  ambiguous)


def _scoreable_trigger(trig):
    """A trigger is scoreable when entry/stop/t1 are present and numeric.

    The audit log has accumulated schema drift — some triggers are stubs like
    {'status': 'armed'} or zones without levels. Those can't be simulated.
    """
    if not isinstance(trig, dict):
        return False
    try:
        for key in ("entry", "stop", "t1"):
            if trig.get(key) is None:
                return False
            float(trig[key])
        if trig.get("t2") is not None:
            float(trig["t2"])
    except (TypeError, ValueError):
        return False
    return True


def apply_replay_to_entry(entry, candles, window_h, fetch_finer=None):
    """Score every scoreable trigger on an audit entry. None if nothing is."""
    if entry.get("outcome") is not None:
        return None
    if entry.get("mode") != "ENTRY":
        return None
    # Passive-fade trades resolve manually and fast (full exit to the mean /
    # time-stop), which the directional 50/50-ladder simulator does NOT model —
    # scoring them here would be wrong. Skip: manual `/scalp resolve` feeds the
    # passive expectancy gate. (Non-action passive rows still get closed out by
    # replay_open_entries' non-action path, since this returns None.)
    if entry.get("setup_family") == "passive-fade":
        return None
    triggers = entry.get("triggers") or {}
    live = {k: v for k, v in triggers.items() if v}
    scoreable = {k: v for k, v in live.items() if _scoreable_trigger(v)}
    if not scoreable:
        return None

    per_trigger = {}
    for label, trig in sorted(scoreable.items()):
        per_trigger[label] = simulate_trigger(trig, entry["side"], candles,
                                              fetch_finer=fetch_finer)

    fired_rs = [t["r"] for t in per_trigger.values() if t["r"] is not None]
    return {
        "window_h": window_h,
        "interval": SCAN_INTERVAL,
        "per_trigger": per_trigger,
        "unscoreable": sorted(set(live) - set(scoreable)),
        "best_r": round(max(fired_rs), 4) if fired_rs else None,
    }


# ------------------------------------------------------------------- fetch

def fetch_candles_ms(coin, interval, interval_ms, start_ms, end_ms):
    """Raw HL candles with epoch-ms timestamps, chunked under the API cap."""
    out, seen = [], set()
    step = MAX_CANDLES_PER_REQ * interval_ms
    chunk_start = start_ms
    while chunk_start < end_ms:
        chunk_end = min(chunk_start + step, end_ms)
        rows = _post_json(HL_INFO, {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": chunk_start, "endTime": chunk_end}}, "hyperliquid")
        for k in rows:
            t = int(k["t"])
            if t not in seen:
                seen.add(t)
                out.append({"t": t, "o": float(k["o"]), "h": float(k["h"]),
                            "l": float(k["l"]), "c": float(k["c"])})
        chunk_start = chunk_end
    out.sort(key=lambda c: c["t"])
    return out


def _make_drill_fetcher(coin):
    def fetch_finer(start_ms, end_ms):
        try:
            return fetch_candles_ms(coin, DRILL_INTERVAL, 60_000,
                                    start_ms, end_ms)
        except Exception:
            return None  # old 1m data pruned server-side -> conservative path
    return fetch_finer


# --------------------------------------------------------------------- CLI

def replay_open_entries(window_h=72, force=False, only_id=None):
    now_ms = int(time.time() * 1000)
    results, skipped = [], []
    for entry in audit_log._load_entries():
        tid = entry.get("id")
        if only_id and tid != only_id:
            continue
        if entry.get("counterfactual") and not force:
            continue
        end_ms = min(int(entry["ts_ms"]) + window_h * 3_600_000, now_ms)
        if end_ms <= int(entry["ts_ms"]):
            skipped.append({"id": tid, "reason": "window empty"})
            continue
        coin = entry["coin"]
        try:
            candles = fetch_candles_ms(coin, SCAN_INTERVAL, SCAN_INTERVAL_MS,
                                       int(entry["ts_ms"]), end_ms)
        except Exception as exc:
            skipped.append({"id": tid, "reason": f"candles unavailable: {exc}"})
            continue
        cf = apply_replay_to_entry(entry, candles, window_h,
                                   fetch_finer=_make_drill_fetcher(coin))
        if cf is None:
            # non-action ENTRY rows with only stub triggers can never be
            # scored — close them out so list-open stops reporting them
            if (entry.get("outcome") is None
                    and entry.get("mode") == "ENTRY"
                    and entry.get("verdict") not in audit_log.ACTION_VERDICTS):
                live = {k for k, v in (entry.get("triggers") or {}).items() if v}
                audit_log.attach_counterfactual(tid, {
                    "window_h": window_h, "interval": SCAN_INTERVAL,
                    "per_trigger": {}, "unscoreable": sorted(live),
                    "best_r": None, "simulated_at_ms": now_ms,
                    "simulated_at_utc": iso_utc(now_ms),
                }, force=force)
                skipped.append({"id": tid, "reason":
                                "closed out — no scoreable triggers "
                                f"(unscoreable: {sorted(live)})"})
            else:
                skipped.append({"id": tid, "reason": "not scoreable "
                                "(resolved / MANAGE / no triggers)"})
            continue
        cf["simulated_at_ms"] = now_ms
        cf["simulated_at_utc"] = iso_utc(now_ms)
        audit_log.attach_counterfactual(tid, cf, force=force)
        results.append({"id": tid, "verdict": entry.get("verdict"),
                        "coin": coin, "side": entry.get("side"),
                        "counterfactual": cf})
    return {"scored": results, "skipped": skipped}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-h", type=int, default=72)
    ap.add_argument("--force", action="store_true",
                    help="re-score entries that already have a counterfactual")
    ap.add_argument("--id", default=None, help="score a single trade_id")
    args = ap.parse_args(argv[1:])
    out = replay_open_entries(window_h=args.window_h, force=args.force,
                              only_id=args.id)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
