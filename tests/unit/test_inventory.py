"""
Unit tests for inventory/manager.py.

Verifies value-weighted skew formula, halt/resume thresholds, quote offset
direction, and apply_fill correctness.
"""

import pytest

from inventory.manager import (
    InventoryState,
    apply_fill,
    quote_offset_ticks,
    should_halt,
    should_resume,
    value_weighted_skew,
)


# ── value_weighted_skew ───────────────────────────────────────────────────────

def test_skew_zero_when_no_position():
    state = InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.5)
    assert value_weighted_skew(state) == pytest.approx(0.0)


def test_skew_zero_when_yes_value_equals_no_value():
    """50 YES at p=0.60 = $30; 75 NO at (1-0.60)=$0.40 = $30 → skew=0."""
    state = InventoryState(yes_shares=50.0, no_shares=75.0, yes_price=0.60)
    assert value_weighted_skew(state) == pytest.approx(0.0)


def test_skew_positive_when_overweight_yes():
    state = InventoryState(yes_shares=100.0, no_shares=0.0, yes_price=0.5)
    assert value_weighted_skew(state) > 0.0


def test_skew_negative_when_overweight_no():
    state = InventoryState(yes_shares=0.0, no_shares=100.0, yes_price=0.5)
    assert value_weighted_skew(state) < 0.0


def test_skew_range_bounded():
    """Skew must always be in [-1.0, 1.0]."""
    state = InventoryState(yes_shares=1000.0, no_shares=0.0, yes_price=0.99)
    s = value_weighted_skew(state)
    assert -1.0 <= s <= 1.0


def test_skew_pure_yes_at_midpoint():
    """100 YES, 0 NO, p=0.5 → YES_value=50, NO_value=0 → skew=1.0."""
    state = InventoryState(yes_shares=100.0, no_shares=0.0, yes_price=0.5)
    assert value_weighted_skew(state) == pytest.approx(1.0)


def test_skew_pure_no_at_midpoint():
    """0 YES, 100 NO, p=0.5 → YES_value=0, NO_value=50 → skew=-1.0."""
    state = InventoryState(yes_shares=0.0, no_shares=100.0, yes_price=0.5)
    assert value_weighted_skew(state) == pytest.approx(-1.0)


def test_skew_value_weighted_not_share_count():
    """KEY CORRECTNESS TEST: 100 YES at p=0.95 must differ from 100 YES at p=0.50.

    A share-count formula would return the same result (1.0) in both cases.
    The value-weighted formula must produce different values.
    """
    state_high = InventoryState(yes_shares=100.0, no_shares=50.0, yes_price=0.95)
    state_mid = InventoryState(yes_shares=100.0, no_shares=50.0, yes_price=0.50)

    skew_high = value_weighted_skew(state_high)
    skew_mid = value_weighted_skew(state_mid)

    # Values must differ — pure share-count would give identical results
    assert skew_high != pytest.approx(skew_mid), (
        "Formula is not value-weighted: same share count produced identical skew "
        "at different prices."
    )

    # Verify the math:
    # state_high: YES=100×0.95=95, NO=50×0.05=2.5  → skew=(95-2.5)/97.5 ≈ 0.949
    # state_mid:  YES=100×0.50=50, NO=50×0.50=25   → skew=(50-25)/75    ≈ 0.333
    assert skew_high == pytest.approx((95 - 2.5) / 97.5)
    assert skew_mid == pytest.approx((50 - 25) / 75)


def test_skew_formula_exact_values():
    """Direct formula check: YES=60, NO=40, p=0.70."""
    # YES_value = 60 × 0.70 = 42
    # NO_value  = 40 × 0.30 = 12
    # skew = (42 - 12) / (42 + 12) = 30 / 54 ≈ 0.5556
    state = InventoryState(yes_shares=60.0, no_shares=40.0, yes_price=0.70)
    assert value_weighted_skew(state) == pytest.approx(30 / 54)


# ── should_halt / should_resume ───────────────────────────────────────────────

def test_halt_triggers_at_threshold():
    """Halt when |skew| >= INVENTORY_HALT_THRESHOLD (default 0.80)."""
    assert should_halt(skew=0.80, halt_threshold=0.80) is True


def test_halt_triggers_above_threshold():
    assert should_halt(skew=0.85, halt_threshold=0.80) is True


def test_halt_not_triggered_below_threshold():
    assert should_halt(skew=0.79, halt_threshold=0.80) is False


def test_halt_triggered_for_negative_skew():
    """Halt is symmetric — applies to both YES-overweight and NO-overweight."""
    assert should_halt(skew=-0.82, halt_threshold=0.80) is True


def test_resume_below_threshold():
    """Resume when |skew| < INVENTORY_RESUME_THRESHOLD (default 0.70)."""
    assert should_resume(skew=0.65, resume_threshold=0.70) is True


def test_resume_not_at_threshold():
    """At exactly the threshold, resume does NOT trigger (strict less-than)."""
    assert should_resume(skew=0.70, resume_threshold=0.70) is False


def test_resume_not_above_threshold():
    assert should_resume(skew=0.75, resume_threshold=0.70) is False


def test_resume_for_negative_skew():
    assert should_resume(skew=-0.60, resume_threshold=0.70) is True


# ── quote_offset_ticks ────────────────────────────────────────────────────────

def test_offset_positive_when_overweight_yes():
    """Overweight YES → positive offset → bid lowered, ask raised."""
    offset = quote_offset_ticks(skew=0.65, multiplier=3)
    assert offset > 0


def test_offset_negative_when_overweight_no():
    """Overweight NO → negative offset → bid raised, ask lowered."""
    offset = quote_offset_ticks(skew=-0.65, multiplier=3)
    assert offset < 0


def test_offset_zero_at_zero_skew():
    assert quote_offset_ticks(skew=0.0, multiplier=3) == 0


def test_offset_rounds_to_integer():
    # skew=0.5, multiplier=3 → 0.5×3=1.5 → round=2
    assert quote_offset_ticks(skew=0.5, multiplier=3) == 2


def test_offset_with_default_multiplier():
    # PRD default INVENTORY_SKEW_MULTIPLIER=3
    # skew=0.65 → round(0.65×3)=round(1.95)=2
    assert quote_offset_ticks(skew=0.65, multiplier=3) == 2


# ── apply_fill ────────────────────────────────────────────────────────────────

def test_apply_fill_buy_increases_yes_shares():
    state = InventoryState(yes_shares=10.0, no_shares=5.0, yes_price=0.6)
    new_state = apply_fill(state, side="BUY", size=20.0)
    assert new_state.yes_shares == pytest.approx(30.0)
    assert new_state.no_shares == pytest.approx(5.0)


def test_apply_fill_sell_increases_no_shares():
    state = InventoryState(yes_shares=10.0, no_shares=5.0, yes_price=0.6)
    new_state = apply_fill(state, side="SELL", size=15.0)
    assert new_state.no_shares == pytest.approx(20.0)
    assert new_state.yes_shares == pytest.approx(10.0)


def test_apply_fill_preserves_yes_price():
    state = InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.75)
    new_state = apply_fill(state, side="BUY", size=10.0)
    assert new_state.yes_price == pytest.approx(0.75)


def test_apply_fill_returns_new_state_not_mutated():
    """apply_fill must not mutate the original state."""
    original = InventoryState(yes_shares=10.0, no_shares=5.0, yes_price=0.5)
    apply_fill(original, side="BUY", size=50.0)
    assert original.yes_shares == pytest.approx(10.0)


def test_apply_fill_case_insensitive():
    state = InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.5)
    new_state = apply_fill(state, side="buy", size=5.0)
    assert new_state.yes_shares == pytest.approx(5.0)


# ── halt/resume integration ───────────────────────────────────────────────────

def test_halt_resume_cycle():
    """Simulate a skew build-up and recovery cycle using PRD defaults."""
    halt_threshold = 0.80
    resume_threshold = 0.70

    # Build up YES overweight until halt triggers
    state = InventoryState(yes_shares=200.0, no_shares=10.0, yes_price=0.90)
    skew = value_weighted_skew(state)
    # YES_value=180, NO_value=1 → skew=179/181 ≈ 0.989 → halt
    assert should_halt(skew, halt_threshold) is True

    # After rebalancing, skew drops
    state_balanced = InventoryState(yes_shares=50.0, no_shares=50.0, yes_price=0.60)
    skew_balanced = value_weighted_skew(state_balanced)
    # YES=30, NO=20, skew=10/50=0.2 → resume
    assert should_resume(skew_balanced, resume_threshold) is True
