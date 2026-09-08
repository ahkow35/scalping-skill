"""Trading-cost helpers shared by live admission and replay scoring."""

TAKER_FEE = 0.00045
SLIPPAGE_FRAC = 0.0002
COST_PER_FILL = TAKER_FEE + SLIPPAGE_FRAC
ROUND_TRIP_FILLS = 2.0


def risk_per_unit(entry, stop):
    """Price-stop distance only; excludes execution costs."""
    return abs(float(entry) - float(stop))


def loss_per_unit(entry, stop):
    """Modeled stopped loss per coin, including round-trip costs.

    Both fills use entry notional, matching the existing replay cost model.
    This is a sizing estimate, not a guaranteed maximum loss on a gapping stop.
    """
    return (risk_per_unit(entry, stop)
            + ROUND_TRIP_FILLS * COST_PER_FILL * abs(float(entry)))


def gross_rr(entry, stop, target):
    risk = risk_per_unit(entry, stop)
    if risk <= 0:
        return 0.0
    return abs(float(target) - float(entry)) / risk


def cost_r(entry, stop):
    """Round-trip trading cost divided by original price-stop distance."""
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


def net_reward_r(entry, stop, target):
    """Net target payoff in original price-stop R (the legacy net_rr value)."""
    return gross_rr(entry, stop, target) - cost_r(entry, stop)


def net_rr(entry, stop, target):
    """After-cost target reward / after-cost stopped loss.

    Callers must validate target direction and stop geometry before admission.
    """
    if risk_per_unit(entry, stop) <= 0:
        return 0.0
    return net_reward_r(entry, stop, target) / (1.0 + cost_r(entry, stop))


def round_trip_cost_r(levels):
    """Compatibility helper for replay's pre-existing level dict shape."""
    return cost_r(levels["entry"], levels["entry"] - levels["risk"])
