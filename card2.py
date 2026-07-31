"""scalp2 card logic: mechanical setup derivation + hard discipline rules.

No alpha claims: `lean` is a label derived from the OI/price/sweep read
(cheatsheet quadrants); the human decides. Every rule here is spec §6.
"""

FEE_TAKER_PCT = 0.045    # % notional per side — base tier, UNVERIFIED
FEE_MAKER_PCT = 0.015
RR_FLOOR = 2.0
LEVERAGE = 10
TIME_STOP_MIN = 60
MAX_CARDS = 3
STOP_BUFFER_ATR = 0.25
ENTRY_ZONE_ATR = 0.15
MAX_STOP_DIST_PCT = 3.0
LEAN_MIN_PRICE_CHG_PCT = 0.10   # window price move below this = no lean
LEAN_MIN_OI_CHG_PCT = 0.20     # window OI move below this = no lean


def derive_lean(price_chg_pct, oi_chg_pct, struct_15m):
    if struct_15m.get("sweep_reclaim"):
        return "long"
    if struct_15m.get("sweep_rejection"):
        return "short"
    if abs(price_chg_pct) < LEAN_MIN_PRICE_CHG_PCT:
        return None
    if oi_chg_pct < LEAN_MIN_OI_CHG_PCT:
        return None                      # no new leverage entering — no lean
    return "long" if price_chg_pct > 0 else "short"


def _mirror(struct, key_f, key_c, side):
    """floors/ceilings swap for shorts so one code path serves both sides."""
    return (struct[key_f], struct[key_c]) if side == "long" \
        else (struct[key_c], struct[key_f])


def derive_setup(lean, struct_15m, struct_1h, mark):
    if lean not in ("long", "short"):
        return None
    if struct_15m.get("atr") is None or struct_1h.get("atr") is None:
        return None                      # no ATR — no structural stop/zone math
    sign = 1 if lean == "long" else -1
    supports15, resists15 = _mirror(struct_15m, "floors", "ceilings", lean)
    supports1h, _ = _mirror(struct_1h, "floors", "ceilings", lean)

    # entry = nearest tested 15m level on the pullback side of price
    entries = [l for l in supports15 if sign * (mark - l["price"]) > 0]
    if not entries:
        return None
    entry = max(entries, key=lambda l: sign * l["price"])["price"]

    # stop = next 1h structural level beyond entry, minus/plus ATR buffer
    pools = [l for l in supports1h if sign * (entry - l["price"]) > 0]
    if not pools:
        return None
    pool = max(pools, key=lambda l: sign * l["price"])["price"]
    stop = pool - sign * STOP_BUFFER_ATR * struct_1h["atr"]
    if abs(entry - stop) / entry * 100 > MAX_STOP_DIST_PCT:
        return None                      # no structural stop within range

    # targets = tested levels beyond entry, nearest first (15m then 1h merge)
    tgt_pool = resists15 + _mirror(struct_1h, "floors", "ceilings", lean)[1]
    tgts = sorted({l["price"] for l in tgt_pool
                   if sign * (l["price"] - entry) > 0},
                  key=lambda p: sign * p)
    if len(tgts) < 2:
        return None
    zone_half = ENTRY_ZONE_ATR * struct_15m["atr"]
    return {"entry": entry, "zone": (entry - zone_half, entry + zone_half),
            "stop": stop, "t1": tgts[0], "t2": tgts[1]}


def rr_net(side, entry, stop, t1, t2):
    sign = 1 if side == "long" else -1
    fee_rt = (FEE_MAKER_PCT + FEE_TAKER_PCT) / 100 * entry
    reward = 0.5 * sign * (t1 - entry) + 0.5 * sign * (t2 - entry) - fee_rt
    risk = sign * (entry - stop) + fee_rt
    return reward / risk


def size_from_risk(equity, risk_pct, entry, stop):
    stop_frac = abs(entry - stop) / entry
    notional = equity * (risk_pct / 100) / stop_frac
    return {"notional_usdc": notional,
            "margin_usdc": notional / LEVERAGE}


# --- gates (spec §5.2, §8) ---------------------------------------------

FUNDING_VETO_8H_PCT = 0.03      # validated 2026-06-10
SESSION_STOP_LOSSES = 2
SESSION_STOP_WINDOW_MS = 12 * 3600_000
EVIDENCE_MIN_RESOLVED = 40
RISK_PCT_PROVING = 0.5
RISK_PCT_PROVEN = 1.0
ATR_SPIKE_MULT = 2.0


def macro_gate(btc_struct_1h, btc_candles_1h, funding_8h_pct):
    """`veto_reasons[side]` holds the cause phrases behind that side's veto,
    so a NO-TRADE line can name the binding condition instead of just
    reporting that one exists. `funding_8h_pct` is BTC's, not the coin's."""
    causes = {"long": [], "short": []}
    size_mult, flags = 1.0, []
    cls = btc_struct_1h.get("classification")
    if cls == "breakdown":
        causes["long"].append("BTC 1h structural breakdown")
    if cls == "breakout":
        causes["short"].append("BTC 1h structural breakout")
    if funding_8h_pct >= FUNDING_VETO_8H_PCT:
        causes["long"].append(f"BTC funding +{funding_8h_pct:.3f}%/8h extreme")
    if funding_8h_pct <= -FUNDING_VETO_8H_PCT:
        causes["short"].append(f"BTC funding {funding_8h_pct:.3f}%/8h extreme")
    flags += [f"{c} — {side}s vetoed"
              for side in ("long", "short") for c in causes[side]]
    ranges = [(float(c["h"]) - float(c["l"])) / float(c["o"])
              for c in btc_candles_1h]
    if len(ranges) > 20:
        avg = sum(ranges[-21:-1]) / 20
        if avg > 0 and ranges[-1] >= ATR_SPIKE_MULT * avg:
            size_mult = 0.5
            flags.append("BTC 1h range spike ≥2x — size halved (not a veto)")
    return {"veto_long": bool(causes["long"]),
            "veto_short": bool(causes["short"]),
            "veto_reasons": causes, "size_mult": size_mult, "flags": flags}


def _resolved_v2(entries):
    return [e for e in entries
            if e.get("system") == "scan2" and e.get("mode") == "ENTRY"
            and e.get("outcome")]


def session_stop_active(entries, now_ms):
    losses = [e for e in _resolved_v2(entries)
              if float(e["outcome"]["outcome_r"]) < 0
              and now_ms - int(e["outcome"]["resolved_at_ms"])
              <= SESSION_STOP_WINDOW_MS]
    return len(losses) >= SESSION_STOP_LOSSES


def v2_risk_pct(entries):
    resolved = _resolved_v2(entries)
    if len(resolved) < EVIDENCE_MIN_RESOLVED:
        return RISK_PCT_PROVING
    mean_r = sum(float(e["outcome"]["outcome_r"]) for e in resolved) \
        / len(resolved)
    return RISK_PCT_PROVEN if mean_r > 0 else RISK_PCT_PROVING


def build_cards(coin_reads, profile, behavioral, macro, entries, now_ms):
    reasons, cards = [], []
    if behavioral.get("cooldown", {}).get("active"):
        return {"cards": [], "no_trade_reasons":
                ["behavioral cooldown active"] + macro["flags"]}
    if session_stop_active(entries, now_ms):
        return {"cards": [], "no_trade_reasons":
                ["session stop: 2 losses in 12h"] + macro["flags"]}
    if not profile.get("equity"):
        return {"cards": [], "no_trade_reasons":
                ["profile equity unset — run: python3 profile.py set "
                 "equity <value>"]}
    risk_pct = v2_risk_pct(entries)
    for read in coin_reads:
        lean = derive_lean(read["price_chg_pct"], read["oi_chg_pct"],
                           read["struct_15m"])
        if lean is None:
            reasons.append(f"{read['coin']}: no directional lean")
            continue
        if macro[f"veto_{lean}"]:
            why = "; ".join(macro.get("veto_reasons", {}).get(lean, []))
            reasons.append(f"{read['coin']}: macro veto blocks {lean}"
                           + (f" — {why}" if why else ""))
            continue
        funding = read["funding_8h_pct"]
        if (lean == "long" and funding >= FUNDING_VETO_8H_PCT) or \
                (lean == "short" and funding <= -FUNDING_VETO_8H_PCT):
            reasons.append(f"{read['coin']}: own funding {funding:+.3f}%/8h "
                           f"extreme — {lean} vetoed")
            continue
        setup = derive_setup(lean, read["struct_15m"], read["struct_1h"],
                             read["mark"])
        if setup is None:
            reasons.append(f"{read['coin']}: no structural setup "
                           "(entry/stop/targets)")
            continue
        rr = rr_net(lean, setup["entry"], setup["stop"],
                    setup["t1"], setup["t2"])
        if rr < RR_FLOOR:
            reasons.append(f"{read['coin']}: net R:R {rr:.1f} < {RR_FLOOR}")
            continue
        size = size_from_risk(profile["equity"],
                              risk_pct * macro["size_mult"],
                              setup["entry"], setup["stop"])
        if size["margin_usdc"] > profile["equity"]:
            reasons.append(f"{read['coin']}: stop too tight — implied "
                           "margin exceeds equity")
            continue
        cards.append({"coin": read["coin"], "side": lean,
                      "zone": setup["zone"], "stop": setup["stop"],
                      "t1": setup["t1"], "t2": setup["t2"],
                      "rr": round(rr, 2),
                      "margin_usdc": round(size["margin_usdc"], 2),
                      "notional_usdc": round(size["notional_usdc"], 2),
                      "risk_pct": risk_pct * macro["size_mult"],
                      "funding_8h_pct": funding,
                      "flow_line": read["flow_line"],
                      "flags": list(macro["flags"]),
                      "time_stop_min": TIME_STOP_MIN,
                      "invalidation":
                          f"1h close beyond {setup['stop']:.4g}"})
    cards.sort(key=lambda c: -c["rr"])
    return {"cards": cards[:MAX_CARDS], "no_trade_reasons": reasons}
