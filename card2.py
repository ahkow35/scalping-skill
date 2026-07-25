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
