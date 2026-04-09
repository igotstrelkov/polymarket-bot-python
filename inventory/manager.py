"""
Inventory Manager — value-weighted skew per §5.1.5 / FR-306.

Share-count skew ignores mark price and produces misleading signals near
extremes. A YES position of 100 shares at p=0.95 represents ~$95 of expected
value; the same count at p=0.50 represents only ~$50. Value-weighted skew
reflects actual economic exposure.

Formula (PRD §5.1.5):
    YES_value = yes_shares × yes_price
    NO_value  = no_shares × (1 - yes_price)
    skew      = (YES_value - NO_value) / (YES_value + NO_value)

skew ranges from -1.0 to +1.0.
  positive → overweight YES  → lower YES bid, raise NO ask
  negative → overweight NO   → raise YES bid, lower NO ask
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InventoryState:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_price: float = 0.5  # live midpoint from BookStateStore


def value_weighted_skew(state: InventoryState) -> float:
    """Compute value-weighted inventory skew per PRD §5.1.5.

    Returns 0.0 when total value is zero (no position or both sides balanced).
    """
    yes_value = state.yes_shares * state.yes_price
    no_value = state.no_shares * (1.0 - state.yes_price)
    total = yes_value + no_value
    if total == 0.0:
        return 0.0
    return (yes_value - no_value) / total


def quote_offset_ticks(skew: float, multiplier: int) -> int:
    """Tick offset to apply to quotes when skew is active.

    Positive skew (overweight YES) → positive offset → bid lowered, ask raised.
    Negative skew (overweight NO)  → negative offset → bid raised, ask lowered.
    """
    return round(skew * multiplier)


def should_halt(skew: float, halt_threshold: float) -> bool:
    """True when |skew| >= halt_threshold (INVENTORY_HALT_THRESHOLD, default 0.80)."""
    return abs(skew) >= halt_threshold


def should_resume(skew: float, resume_threshold: float) -> bool:
    """True when |skew| < resume_threshold (INVENTORY_RESUME_THRESHOLD, default 0.70)."""
    return abs(skew) < resume_threshold


def apply_fill(state: InventoryState, side: str, size: float) -> InventoryState:
    """Return a new InventoryState updated for a fill event.

    side: 'BUY' increases yes_shares; 'SELL' decreases yes_shares and
    increases no_shares (net short YES = long NO exposure).

    For a binary market, buying YES decreases NO exposure and vice versa.
    Convention: a BUY fill adds YES shares; a SELL fill adds NO shares
    (the system is always on one side per token).
    """
    yes = state.yes_shares
    no = state.no_shares
    side_upper = side.upper()
    if side_upper == "BUY":
        yes += size
    elif side_upper == "SELL":
        no += size
    return InventoryState(yes_shares=yes, no_shares=no, yes_price=state.yes_price)
