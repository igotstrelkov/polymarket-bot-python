"""
Fee calculator — pure functions, no I/O.

FR-153: min_profitable_spread = max(BASE_SPREAD, 2 × fee_rate_decimal + COST_FLOOR)
FR-154: Strategy A additionally requires observed_spread >= 3 × fee_rate_decimal
        when fee_rate_bps > 100. Both FR-153 AND FR-154 must pass.
FR-155: Strategy C hard gate: fee_rate_bps < SNIPE_MAX_FEE_BPS (strict less-than).
"""


def bps_to_decimal(bps: int) -> float:
    """Convert basis points to decimal fraction. 78 bps → 0.0078."""
    return bps / 10_000


def min_profitable_spread(fee_rate_bps: int, base_spread: float, cost_floor: float) -> float:
    """FR-153: max(base_spread, 2 × fee_rate_decimal + cost_floor).

    Example: fee_rate_bps=78, base_spread=0.04, cost_floor=0.01
      fee_decimal = 0.0078
      candidate   = 2 × 0.0078 + 0.01 = 0.0256
      result      = max(0.04, 0.0256) = 0.04
    """
    fee_decimal = bps_to_decimal(fee_rate_bps)
    return max(base_spread, 2 * fee_decimal + cost_floor)


def passes_strategy_a_gate(
    fee_rate_bps: int,
    observed_spread: float,
    base_spread: float,
    cost_floor: float,
) -> bool:
    """FR-153 AND FR-154 must both pass.

    FR-153: observed_spread >= min_profitable_spread(...)
    FR-154: if fee_rate_bps > 100, observed_spread >= 3 × fee_rate_decimal
    """
    min_spread = min_profitable_spread(fee_rate_bps, base_spread, cost_floor)
    if observed_spread < min_spread:
        return False

    # FR-154: additional filter for high-fee markets
    if fee_rate_bps > 100:
        fee_decimal = bps_to_decimal(fee_rate_bps)
        if observed_spread < 3 * fee_decimal:
            return False

    return True


def passes_strategy_c_gate(fee_rate_bps: int, max_fee_bps: int) -> bool:
    """FR-155: strict less-than — fee_rate_bps < max_fee_bps. Not <=."""
    return fee_rate_bps < max_fee_bps
