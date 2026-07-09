"""Deterministic decision orchestrator for the scalp skill's live bot.

Ports what the LLM does across scalp-core.md + the direction module into a
single pure function: behavioral gate -> macro veto -> structure -> triggers ->
Step 1b flow conviction cuts -> Step 4b verdict mapping -> Step 6 sizing.
The loop (scalp_loop.py) and the backtest (backtest.py) both call decide().

Scope: ENTRY mode only (fresh-thesis alerts). MANAGE mode stays with the
interactive skill for now. R16 (psychology) is deliberately absent — it is
about the trader, not the market, and ships as the human checklist in the
alert. Event-day handling is flag-only (per the v1 decision): no hard veto, an
informational event_flag when a blackout date is configured.

Never raises; returns a JSON-serialisable verdict dict shaped like the audit
payload so audit_log.py can log it unchanged.
"""

import costs
import structure
import triggers

# Conviction tiers as integers so cuts compose by min().
_TIER = {3: "high", 2: "med", 1: "low"}
_VERDICT = {
    ("long", 3): "LONG-NOW", ("long", 2): "LONG-CLOSE", ("long", 1): "LONG-PROBE",
    ("short", 3): "SHORT-NOW", ("short", 2): "SHORT-CLOSE", ("short", 1): "SHORT-PROBE",
}
_SIZE_MULT = {3: 1.0, 2: 0.5, 1: 0.25}

DEFAULT_PARAMS = {
    "funding_veto_long": 0.0003,    # +0.03%/8h => crowded long, veto longs
    "funding_veto_short": -0.0003,  # -0.03%/8h => crowded short, veto shorts
    "btc_d_veto_long": 1.5,         # BTC.D 24h chg >= +1.5 & rising => veto longs
    "btc_d_veto_short": -1.5,       # BTC.D 24h chg <= -1.5 & falling => veto shorts
    "cap_phase1": 0.005,            # 0.5% risk cap
    "cap_phase2": 0.02,             # 2.0% risk cap
    "use_closed_only": True,        # drop the forming bar for confirmation
    "structure_tf": "15m",
}


# ---- helpers ----

def _closed(candles, p):
    if p["use_closed_only"] and candles and len(candles) > 1:
        return candles[:-1]
    return candles


def _volume_rising(candles, recent=3, base=20):
    vols = [float(k.get("v", 0.0)) for k in candles]
    if len(vols) < recent + 1:
        return False
    r = sum(vols[-recent:]) / recent
    b = sum(vols[-base:]) / min(base, len(vols))
    return b > 0 and r > b


def _btc_structural_break(btc_candles, side, p):
    """Long is vetoed by a BTC breakdown on rising volume; short by a BTC
    breakout on rising volume. Uses the same structure engine on BTC."""
    bs = structure.classify_structure(btc_candles, params={"min_touches": 2})
    rising = _volume_rising(btc_candles)
    if side == "long" and bs["classification"] == "breakdown" and rising:
        return "BTC broke structure down on rising volume"
    if side == "short" and bs["classification"] == "breakout" and rising:
        return "BTC broke structure up on rising volume"
    return None


def _macro(side, market, p, econ_blackout):
    """Direction-aware macro veto + size modifier. Returns a dict with status
    (CLEAR / VETO / NO-TRADE), size_mult, and reasons."""
    reasons = []
    ctx = market.get("ctx", {})
    btc = market.get("btc_ctx", {})
    dom = market.get("btc_dominance", {})
    session = market.get("session", {})

    if not market.get("macro_can_clear", True):
        return {"status": "NO-TRADE", "size_mult": 0.0,
                "reasons": ["BTC.D snapshot unavailable — veto cannot clear"],
                "bias": "unknown", "event_flag": None}

    # Event day: flag only, no veto (v1 decision).
    event_flag = None
    today = str(session.get("utc", ""))[:10]
    if econ_blackout and today in set(econ_blackout):
        event_flag = "US econ event today — bot does not veto; apply judgement"

    # Funding extreme (crowded book against the side).
    funding = ctx.get("funding")
    if funding is not None:
        if side == "long" and funding > p["funding_veto_long"]:
            reasons.append(f"funding +{funding*100:.4f}%/8h — crowded long")
        if side == "short" and funding < p["funding_veto_short"]:
            reasons.append(f"funding {funding*100:.4f}%/8h — crowded short")

    # BTC structural break on rising volume.
    brk = _btc_structural_break(market.get("btc_candles", {}).get(p["structure_tf"], []), side, p)
    if brk:
        reasons.append(brk)

    # BTC.D component (skipped when 24h change unavailable).
    d_chg = dom.get("btc_d_24h_chg")
    if d_chg is not None:
        if side == "long" and d_chg >= p["btc_d_veto_long"]:
            reasons.append(f"BTC.D +{d_chg:.2f}% rising — alt headwind")
        if side == "short" and d_chg <= p["btc_d_veto_short"]:
            reasons.append(f"BTC.D {d_chg:.2f}% falling — alts ripping")

    if reasons:
        return {"status": "VETO", "size_mult": 0.0, "reasons": reasons,
                "bias": "veto", "event_flag": event_flag}

    # Size modifier from BTC direction + BTC.D drift (headwind => half size).
    size_mult, bias = 1.0, "neutral"
    btc_up = btc.get("mark") is not None and btc.get("prev_day_px") and btc["mark"] > btc["prev_day_px"]
    d_rising = d_chg is not None and d_chg > 0
    if side == "long" and d_rising and btc_up:
        size_mult, bias = 0.5, "headwind"
    if side == "short" and d_rising and not btc_up:
        size_mult, bias = 1.0, "tailwind"
    if side == "short" and (d_chg is not None and d_chg < 0) and btc_up:
        size_mult, bias = 0.5, "headwind"
    # Weekend modifier (short only).
    if side == "short" and session.get("weekend_window"):
        size_mult *= 0.5
        bias = bias + "+weekend"
    return {"status": "CLEAR", "size_mult": size_mult, "reasons": [],
            "bias": bias, "event_flag": event_flag}


def _flow_cuts(side, flow, trigger_name):
    """Step 1b: conviction only ever gets CUT. Returns (delta_tiers, notes)."""
    if not isinstance(flow, dict):
        return -2, ["flow unavailable — treat as low coverage"]
    cut, notes = 0, []
    if not flow.get("coverage_ok", False):
        return -2, [f"low coverage {flow.get('max_coverage_pct')}% — half-blind, cap low"]
    bias = flow.get("aggressor_bias")
    want = "buyers" if side == "long" else "sellers"
    if bias == want:
        pass
    elif bias == "balanced":
        cut -= 1
        notes.append("flow balanced — no confirmation")
    else:
        cut -= 2
        notes.append(f"flow opposes ({bias})")
    div = flow.get("delta_divergence")
    if (side == "long" and div == "bearish") or (side == "short" and div == "bullish"):
        cut -= 1
        notes.append(f"delta divergence {div} — exhaustion")
    climax = flow.get("volume_climax") or {}
    cdir = climax.get("direction") if isinstance(climax, dict) else None
    if (side == "long" and cdir == "up") or (side == "short" and cdir == "down"):
        cut -= 1
        notes.append(f"climax {cdir} in entry direction — late")
    if trigger_name and trigger_name.endswith("_B") and side == "long" and not flow.get("breakout_vol_ok", True):
        cut -= 1
        notes.append("breakout on thin volume")
    return cut, notes


def _pick_trigger(trig):
    """Prefer a fired trigger that passes the R:R floor; among those, highest
    net R:R on T1. Returns (letter, block) or (None, None)."""
    fired = [(k, v) for k, v in trig.items() if v.get("fired")]
    passing = [(k, v) for k, v in fired if v.get("passes_rr")]
    pool = passing or []
    if not pool:
        return None, None
    letter, block = max(pool, key=lambda kv: kv[1]["net_rr_t1"])
    return letter, block


def _sizing(equity, cap, tier, entry, stop, size_mult):
    mult = _SIZE_MULT[tier] * size_mult
    risk = equity * cap * mult
    dist = abs(entry - stop)
    if dist <= 0 or equity <= 0:
        return None
    coins = risk / dist
    return {
        "risk_usdc": round(risk, 2), "coins": round(coins, 4),
        "leverage": round(coins * entry / equity, 2),
        "cap_pct": round(cap * 100, 3), "tier_mult": _SIZE_MULT[tier],
        "macro_size_mult": size_mult, "stop_dist": round(dist, 6),
    }


def decide(side, market, behavioral_state, profile, *, params=None, econ_blackout=None):
    """Return a deterministic ENTRY verdict for `side` on `market`.

    market: fetch_market.py output. behavioral_state: behavioral.py output.
    profile: {equity, phase}. Pure; never raises.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    coin = market.get("primary") or market.get("ctx", {}).get("coin")
    mid = market.get("ctx", {}).get("mid")
    out = {
        "coin": coin, "side": side, "mode": "ENTRY", "verdict": "WAIT",
        "conviction": "low", "macro_status": "CLEAR", "trigger_used": None,
        "triggers": {"A": None, "B": None}, "sizing": None, "flags": {},
        "reason": "", "mid": mid,
    }

    # Step 0 — behavioral daily-stop halt (ENTRY only).
    bs = behavioral_state or {}
    ds = bs.get("daily_stop", {})
    if ds.get("active"):
        out.update(verdict="HALT", macro_status="HALT",
                   reason=f"daily stop active ({ds.get('reason')}) until {ds.get('until_utc')}")
        out["flags"]["daily_stop"] = ds
        return out
    if bs.get("cooldown", {}).get("active"):
        out["flags"]["cooldown"] = "active — informational only"

    # Step 2 — macro veto.
    macro = _macro(side, market, p, econ_blackout)
    out["macro_status"] = macro["status"]
    out["flags"]["macro_bias"] = macro["bias"]
    if macro["event_flag"]:
        out["flags"]["event"] = macro["event_flag"]
    if macro["status"] in ("VETO", "NO-TRADE"):
        out.update(verdict=("NO-TRADE" if macro["status"] == "NO-TRADE" else "VETOED"),
                   conviction="n/a", reason="; ".join(macro["reasons"]))
        return out

    # Steps 3–4 — structure + triggers on the confirmation timeframe.
    candles = _closed(market.get("candles", {}).get(p["structure_tf"], []), p)
    if mid is None or not candles:
        out["reason"] = "no price/candles"
        return out
    s = structure.classify_structure(candles, price=mid)
    trig = triggers.evaluate(side, s, candles)
    out["triggers"] = trig
    letter, block = _pick_trigger(trig)
    out["flags"]["structure"] = s["classification"]
    if not block:
        # Surface why: nearest non-fired reason is the most informative.
        why = next((v["reason"] for v in trig.values() if not v["fired"]), "no trigger")
        out["reason"] = f"no trigger passing R:R — {why}"
        return out

    # Step 1b — flow conviction cuts (start high, only ever cut).
    tier = 3
    cut, notes = _flow_cuts(side, market.get("flow"), block["trigger"])
    tier = max(1, tier + cut)
    out["conviction"] = _TIER[tier]
    out["flags"]["flow_notes"] = notes

    # Step 4b + 6 — verdict + sizing.
    equity = (profile or {}).get("equity")
    phase = (profile or {}).get("phase", 1)
    cap = p["cap_phase2"] if phase == 2 else p["cap_phase1"]
    out["verdict"] = _VERDICT[(side, tier)]
    out["trigger_used"] = letter
    if equity:
        out["sizing"] = _sizing(equity, cap, tier, block["entry"], block["stop"], macro["size_mult"])
    else:
        out["flags"]["sizing"] = "equity unset — cannot size"
    out["reason"] = f"{block['trigger']} fired ({block['reason']}); net R:R T1 {block['net_rr_t1']}"
    return out
