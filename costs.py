"""Trading-cost helpers shared by live admission and replay scoring."""

TAKER_FEE = 0.00045
SLIPPAGE_FRAC = 0.0002
COST_PER_FILL = TAKER_FEE + SLIPPAGE_FRAC
ROUND_TRIP_FILLS = 2.0


def risk_per_unit(entry, stop):
    return abs(float(entry) - float(stop))


def gross_rr(entry, stop, target):
    risk = risk_per_unit(entry, stop)
    if risk <= 0:
        return 0.0
    return abs(float(target) - float(entry)) / risk


def cost_r(entry, stop):
    """Round-trip trading cost for one trade, in R units."""
    risk = risk_per_unit(entry, stop)
    if risk <= 0:
        return 0.0
    return ROUND_TRIP_FILLS * COST_PER_FILL * abs(float(entry)) / risk


def execution_cost_r(entry, stop, spread_bps=None):
    """Live execution cost in R, adding half-spread when book data is present."""
    extra = 0.0
    if spread_bps is not None:
        extra = max(0.0, float(spread_bps)) / 10000 / 2
    risk = risk_per_unit(entry, stop)
    if risk <= 0:
        return 0.0
    return cost_r(entry, stop) + extra * abs(float(entry)) / risk


def net_rr(entry, stop, target):
    return gross_rr(entry, stop, target) - cost_r(entry, stop)


def round_trip_cost_r(levels):
    """Compatibility helper for replay's pre-existing level dict shape."""
    return cost_r(levels["entry"], levels["entry"] - levels["risk"])
