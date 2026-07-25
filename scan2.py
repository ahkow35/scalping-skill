"""scalp2 scanner orchestrator. Deterministic — no LLM in this path.

Warm window: two assemble() pulls `window_sec` apart give a measured
OI/price/flow delta over the scan's own window (kills v1's cold-cache
blindness). Card logic lives in card2; this file only orchestrates,
renders, notifies, and logs.
"""
import argparse
import os
import subprocess
import time

import audit_log
import card2
import structure
import universe2
from behavioral import compute_behavioral_state
from fetch_market import assemble as _assemble, iso_utc, DataUnavailable
from profile import load_profile

CARDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards")


def _flow_line(td15):
    sample = td15.get("sample_pct")
    delta = td15.get("delta_usdc")
    if sample is None or delta is None:
        return "flow: DATA UNAVAILABLE"
    tag = "" if sample >= 50 else " LOW-SAMPLE"
    side = "buy" if delta > 0 else "sell"
    return f"flow {side} ${abs(delta):,.0f}/15m (sample {sample:.0f}%{tag})"


def _coin_read(coin, snap_a, snap_b):
    mark_a, mark_b = snap_a["ctx"]["mark"], snap_b["ctx"]["mark"]
    oi_a, oi_b = snap_a["ctx"]["oi_usdc"], snap_b["ctx"]["oi_usdc"]
    return {
        "coin": coin, "mark": mark_b,
        "struct_15m": structure.classify_structure(
            snap_b["candles"]["15m"], price=mark_b),
        "struct_1h": structure.classify_structure(
            snap_b["candles"]["1h"], price=mark_b),
        "price_chg_pct": (mark_b / mark_a - 1) * 100,
        "oi_chg_pct": (oi_b / oi_a - 1) * 100,
        "funding_8h_pct": snap_b["ctx"]["funding"] * 8 * 100,
        "flow_line": _flow_line(snap_b["taker_delta"]["15m"]),
    }


def render(result, session, session_info):
    head = f"scalp2 scan [{session}] {session_info.get('utc', '')}"
    lines = [head, "=" * len(head)]
    if not result["cards"]:
        lines.append("NO-TRADE")
        lines += [f"  - {r}" for r in result["no_trade_reasons"]]
        return "\n".join(lines)
    for c in result["cards"]:
        lo, hi = c["zone"]
        lines += [
            f"{c['coin']}  {c['side'].upper()}-lean | retest "
            f"{lo:.4g}-{hi:.4g}, stop {c['stop']:.4g} (structural), "
            f"T1 {c['t1']:.4g}  T2 {c['t2']:.4g}",
            f"  R:R {c['rr']}:1 net | ${c['margin_usdc']:,.0f} margin @"
            f"{card2.LEVERAGE}x (risk {c['risk_pct']:.2g}%) | "
            f"{c['flow_line']}",
            f"  invalidation: {c['invalidation']} | time-stop: exit review "
            f"at +{c['time_stop_min']} min",
        ]
        lines += [f"  ! {f}" for f in c["flags"]]
    if result["no_trade_reasons"]:
        lines.append("skipped: " + "; ".join(result["no_trade_reasons"]))
    return "\n".join(lines)


def _notify(title, body):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}"'],
            timeout=10, check=False)
    except Exception:
        pass                                   # best-effort by design


def run_scan(session, window_sec, coins=None, *, sleep=time.sleep,
             assemble=None, notify=True):
    assemble = assemble or _assemble
    now_ms = int(time.time() * 1000)
    coins = coins or universe2.select_universe(now_ms)
    snaps_a = {c: assemble(c) for c in coins}
    sleep(window_sec)
    snaps_b = {c: assemble(c) for c in coins}
    ref = snaps_b[coins[0]]

    reads = [_coin_read(c, snaps_a[c], snaps_b[c]) for c in coins]
    btc_struct = structure.classify_structure(
        ref["btc_candles"]["1h"], price=ref["btc_ctx"]["mark"])
    macro = card2.macro_gate(btc_struct, ref["btc_candles"]["1h"],
                             ref["btc_ctx"]["funding"] * 8 * 100)
    entries = audit_log._load_entries(None)
    result = card2.build_cards(reads, load_profile(),
                               compute_behavioral_state(), macro,
                               entries, int(time.time() * 1000))
    text = render(result, session, ref.get("session", {}))

    os.makedirs(CARDS_DIR, exist_ok=True)
    day = iso_utc(int(time.time() * 1000))[:10]
    path = os.path.join(CARDS_DIR, f"{day}-{session}.md")
    with open(path, "w") as f:
        f.write(text + "\n")
    audit_log.write_audit_entry({
        "coin": ",".join(coins), "side": "none", "mode": "SCAN",
        "system": "scan2", "session": session,
        "verdict": "CARDS" if result["cards"] else "NO-TRADE",
        "cards": result["cards"],
        "no_trade_reasons": result["no_trade_reasons"]})
    if notify:
        summary = (f"{len(result['cards'])} setup(s)" if result["cards"]
                   else "NO-TRADE")
        _notify(f"scalp2 [{session}]", summary)
    return text


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", default="manual",
                   choices=["us", "asia", "manual"])
    p.add_argument("--window-sec", type=int, default=900)
    p.add_argument("--coins", default=None)
    p.add_argument("--no-notify", action="store_true")
    a = p.parse_args()
    coins = a.coins.split(",") if a.coins else None
    try:
        print(run_scan(a.session, a.window_sec, coins,
                       notify=not a.no_notify))
    except DataUnavailable as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
