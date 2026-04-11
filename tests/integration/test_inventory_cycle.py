"""
Integration tests for inventory management cycle (§5.1.5, FR-306).

Covers:
- Fills drive value-weighted skew toward halt threshold → halt triggered
- Alert dispatched when halt fires
- Skew recovers below resume threshold → quoting resumes
- Full fill cycle with multiple positions
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from inventory.manager import (
    InventoryState,
    apply_fill,
    should_halt,
    should_resume,
    value_weighted_skew,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

HALT_THRESHOLD = 0.80
RESUME_THRESHOLD = 0.70


def skew_after_fills(
    yes_fills: list[float],
    no_fills: list[float],
    yes_price: float = 0.50,
) -> float:
    state = InventoryState(yes_price=yes_price)
    for size in yes_fills:
        state = apply_fill(state, "BUY", size)
    for size in no_fills:
        state = apply_fill(state, "SELL", size)
    state = InventoryState(
        yes_shares=state.yes_shares,
        no_shares=state.no_shares,
        yes_price=yes_price,
    )
    return value_weighted_skew(state)


# ── Test 1: Fills drive skew to halt threshold ────────────────────────────────

def test_empty_inventory_has_zero_skew():
    """No fills → zero skew."""
    state = InventoryState()
    assert value_weighted_skew(state) == pytest.approx(0.0)


def test_buy_fills_produce_positive_skew():
    """BUY fills increase yes_shares → positive skew (overweight YES)."""
    state = InventoryState(yes_price=0.50)
    state = apply_fill(state, "BUY", 10.0)
    state = InventoryState(yes_shares=state.yes_shares, no_shares=state.no_shares, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert skew > 0.0


def test_skew_reaches_halt_threshold():
    """Sufficient BUY fills push |skew| >= halt threshold."""
    # yes_shares=10, no_shares=1, yes_price=0.5:
    # yes_value=5, no_value=0.5, skew=4.5/5.5≈0.818 >= 0.80
    state = InventoryState(yes_shares=10.0, no_shares=1.0, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_halt(skew, HALT_THRESHOLD) is True


def test_halt_not_triggered_below_threshold():
    """Skew below halt threshold → should_halt returns False."""
    # yes_shares=5, no_shares=3, yes_price=0.5:
    # yes_value=2.5, no_value=1.5, skew=1.0/4.0=0.25 < 0.80
    state = InventoryState(yes_shares=5.0, no_shares=3.0, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_halt(skew, HALT_THRESHOLD) is False


def test_halt_triggers_with_negative_skew():
    """Negative skew beyond halt threshold (overweight NO) also halts."""
    # no_shares=10, yes_shares=1, yes_price=0.5:
    # yes_value=0.5, no_value=5.0, skew=(0.5-5)/5.5≈-0.818 → |skew|≥0.80
    state = InventoryState(yes_shares=1.0, no_shares=10.0, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_halt(skew, HALT_THRESHOLD) is True


@pytest.mark.asyncio
async def test_halt_triggers_alert():
    """When halt fires, mock alerter is called."""
    mock_alerter = AsyncMock()
    mock_alerter.inventory_halt = AsyncMock()

    state = InventoryState(yes_shares=10.0, no_shares=1.0, yes_price=0.50)
    skew = value_weighted_skew(state)

    halted = False
    if should_halt(skew, HALT_THRESHOLD) and not halted:
        await mock_alerter.inventory_halt(token_id="tok_1", skew=skew)
        halted = True

    assert halted is True
    mock_alerter.inventory_halt.assert_awaited_once()


def test_full_fill_cycle_halt():
    """Multiple BUY fills accumulate and reach halt threshold."""
    state = InventoryState(yes_price=0.50)

    # Apply 10 BUY fills of 1 share each and 1 SELL
    for _ in range(10):
        state = apply_fill(state, "BUY", 1.0)
    state = apply_fill(state, "SELL", 1.0)

    state = InventoryState(
        yes_shares=state.yes_shares,
        no_shares=state.no_shares,
        yes_price=0.50,
    )
    skew = value_weighted_skew(state)
    # yes=10, no=1, yes_price=0.5: yes_value=5, no_value=0.5, skew=4.5/5.5≈0.818
    assert should_halt(skew, HALT_THRESHOLD) is True


# ── Test 2: Skew recovers below resume threshold → quoting resumes ────────────

def test_skew_recovers_below_resume_threshold():
    """Adding NO fills reduces skew below resume threshold."""
    # Start at halt state
    state = InventoryState(yes_shares=10.0, no_shares=1.0, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_halt(skew, HALT_THRESHOLD) is True

    # Add NO fills to reduce skew
    state = apply_fill(state, "SELL", 2.0)
    state = InventoryState(
        yes_shares=state.yes_shares,
        no_shares=state.no_shares,
        yes_price=0.50,
    )
    # yes=10, no=3, yes_price=0.5: yes_value=5, no_value=1.5, skew=3.5/6.5≈0.538 < 0.70
    skew = value_weighted_skew(state)
    assert should_resume(skew, RESUME_THRESHOLD) is True


def test_resume_threshold_is_below_halt_threshold():
    """Resume threshold (0.70) < halt threshold (0.80) creates hysteresis."""
    assert RESUME_THRESHOLD < HALT_THRESHOLD


def test_skew_between_resume_and_halt_does_not_resume():
    """Skew between resume and halt thresholds: halted but not yet resumed."""
    # yes=8, no=2, yes_price=0.5: yes_value=4, no_value=1, skew=3/5=0.60 < 0.70 → resumes
    # Let's find a value between 0.70 and 0.80
    # yes=10, no=2, yes_price=0.5: yes_value=5, no_value=1, skew=4/6=0.667 < 0.70 → resumes
    # yes=10, no=1.5, yes_price=0.5: yes_value=5, no_value=0.75, skew=4.25/5.75≈0.739 > 0.70 and < 0.80
    state = InventoryState(yes_shares=10.0, no_shares=1.5, yes_price=0.50)
    skew = value_weighted_skew(state)
    # In halt zone? No (< 0.80). In resume zone? No (≥ 0.70)
    assert should_halt(skew, HALT_THRESHOLD) is False
    assert should_resume(skew, RESUME_THRESHOLD) is False


def test_full_recovery_cycle():
    """Complete cycle: fill → halt → reduce position → resume."""
    # Phase 1: accumulate position
    state = InventoryState(yes_price=0.50)
    for _ in range(10):
        state = apply_fill(state, "BUY", 1.0)
    state = apply_fill(state, "SELL", 1.0)

    # Recompute with yes_price
    state = InventoryState(yes_shares=state.yes_shares, no_shares=state.no_shares, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_halt(skew, HALT_THRESHOLD), f"Expected halt at skew={skew:.3f}"

    # Phase 2: reduce position until resume
    state = apply_fill(state, "SELL", 2.0)
    state = InventoryState(yes_shares=state.yes_shares, no_shares=state.no_shares, yes_price=0.50)
    skew = value_weighted_skew(state)
    assert should_resume(skew, RESUME_THRESHOLD), f"Expected resume at skew={skew:.3f}"


def test_apply_fill_buy_increases_yes_shares():
    """BUY fill increases yes_shares, leaves no_shares unchanged."""
    state = InventoryState(yes_shares=5.0, no_shares=2.0, yes_price=0.50)
    new_state = apply_fill(state, "BUY", 3.0)
    assert new_state.yes_shares == pytest.approx(8.0)
    assert new_state.no_shares == pytest.approx(2.0)


def test_apply_fill_sell_increases_no_shares():
    """SELL fill increases no_shares, leaves yes_shares unchanged."""
    state = InventoryState(yes_shares=5.0, no_shares=2.0, yes_price=0.50)
    new_state = apply_fill(state, "SELL", 3.0)
    assert new_state.yes_shares == pytest.approx(5.0)
    assert new_state.no_shares == pytest.approx(5.0)


def test_value_weighted_skew_symmetric():
    """Equal value on both sides → zero skew."""
    # yes=4, no=4, yes_price=0.5: yes_value=2, no_value=2, skew=0
    state = InventoryState(yes_shares=4.0, no_shares=4.0, yes_price=0.50)
    assert value_weighted_skew(state) == pytest.approx(0.0)


def test_skew_uses_mark_price():
    """Value-weighted skew accounts for current mark price."""
    # Same shares, different price → different skew
    # yes=10, no=10, yes_price=0.9: yes_value=9, no_value=1, skew=8/10=0.80
    state_high = InventoryState(yes_shares=10.0, no_shares=10.0, yes_price=0.90)
    skew_high = value_weighted_skew(state_high)

    # yes=10, no=10, yes_price=0.5: yes_value=5, no_value=5, skew=0
    state_mid = InventoryState(yes_shares=10.0, no_shares=10.0, yes_price=0.50)
    skew_mid = value_weighted_skew(state_mid)

    assert skew_high > skew_mid
