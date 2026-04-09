"""
Unit tests for fees/calculator.py.

Covers FR-153, FR-154, FR-155 and the PRD worked example.
"""

import pytest

from fees.calculator import (
    bps_to_decimal,
    min_profitable_spread,
    passes_strategy_a_gate,
    passes_strategy_c_gate,
)


# ── bps_to_decimal ────────────────────────────────────────────────────────────

def test_bps_to_decimal_78():
    assert bps_to_decimal(78) == pytest.approx(0.0078)


def test_bps_to_decimal_0():
    assert bps_to_decimal(0) == pytest.approx(0.0)


def test_bps_to_decimal_10000():
    assert bps_to_decimal(10_000) == pytest.approx(1.0)


# ── min_profitable_spread — FR-153 worked example ────────────────────────────

def test_min_spread_prd_worked_example():
    """PRD FR-153 example: fee_rate_bps=78, base_spread=0.04, cost_floor=0.01.
    fee_decimal = 0.0078
    candidate   = 2 × 0.0078 + 0.01 = 0.0256
    result      = max(0.04, 0.0256) = 0.04
    """
    result = min_profitable_spread(fee_rate_bps=78, base_spread=0.04, cost_floor=0.01)
    assert result == pytest.approx(0.04)


def test_min_spread_fee_dominates():
    """When fee component exceeds base_spread, fee side wins."""
    # 2 × 0.03 + 0.01 = 0.07 > 0.04
    result = min_profitable_spread(fee_rate_bps=300, base_spread=0.04, cost_floor=0.01)
    assert result == pytest.approx(0.07)


def test_min_spread_base_dominates():
    """base_spread wins when fee component is small."""
    result = min_profitable_spread(fee_rate_bps=5, base_spread=0.04, cost_floor=0.01)
    assert result == pytest.approx(0.04)


# ── passes_strategy_a_gate — FR-153 AND FR-154 ───────────────────────────────

def test_strategy_a_passes_when_spread_meets_min():
    """FR-153 alone: fee_rate_bps=78 → min_spread=0.04; observed_spread=0.05 passes."""
    assert passes_strategy_a_gate(
        fee_rate_bps=78, observed_spread=0.05, base_spread=0.04, cost_floor=0.01
    ) is True


def test_strategy_a_fails_when_spread_below_min():
    """Observed spread below min_profitable_spread fails FR-153."""
    assert passes_strategy_a_gate(
        fee_rate_bps=78, observed_spread=0.02, base_spread=0.04, cost_floor=0.01
    ) is False


def test_strategy_a_fr154_not_triggered_when_fee_le_100():
    """FR-154 is only applied when fee_rate_bps > 100."""
    # fee=100, spread=0.04 meets FR-153 (min_spread=max(0.04,0.02+0.01)=0.04)
    # FR-154 not triggered since fee_rate_bps == 100 (not >100)
    assert passes_strategy_a_gate(
        fee_rate_bps=100, observed_spread=0.04, base_spread=0.04, cost_floor=0.01
    ) is True


def test_strategy_a_fr154_fails_when_fee_gt_100_and_spread_too_narrow():
    """FR-154: fee_rate_bps=150, observed_spread must be >= 3 × 0.015 = 0.045."""
    # FR-153: min_spread = max(0.04, 2×0.015+0.01) = max(0.04, 0.04) = 0.04 — passes
    # FR-154: 3 × 0.015 = 0.045 — observed_spread=0.04 FAILS FR-154
    assert passes_strategy_a_gate(
        fee_rate_bps=150, observed_spread=0.04, base_spread=0.04, cost_floor=0.01
    ) is False


def test_strategy_a_fr153_passing_alone_insufficient():
    """FR-153 passing alone is not enough when FR-154 is also required."""
    # fee_rate_bps=200 → min_spread=max(0.04, 0.04+0.01)=0.05
    # observed=0.055 passes FR-153; but 3×0.02=0.06 — fails FR-154
    assert passes_strategy_a_gate(
        fee_rate_bps=200, observed_spread=0.055, base_spread=0.04, cost_floor=0.01
    ) is False


def test_strategy_a_both_fr153_and_fr154_pass():
    """Both gates pass when spread is comfortably wide."""
    # fee=200, decimal=0.02; min_spread=max(0.04,0.05)=0.05; 3x=0.06
    # observed=0.07 passes both
    assert passes_strategy_a_gate(
        fee_rate_bps=200, observed_spread=0.07, base_spread=0.04, cost_floor=0.01
    ) is True


# ── passes_strategy_c_gate — FR-155 ──────────────────────────────────────────

def test_strategy_c_passes_below_threshold():
    """bps=4 passes with SNIPE_MAX_FEE_BPS=5 (strict less-than)."""
    assert passes_strategy_c_gate(fee_rate_bps=4, max_fee_bps=5) is True


def test_strategy_c_fails_at_threshold():
    """bps=5 fails with SNIPE_MAX_FEE_BPS=5 — hard gate is strict less-than."""
    assert passes_strategy_c_gate(fee_rate_bps=5, max_fee_bps=5) is False


def test_strategy_c_fails_above_threshold():
    """bps=6 fails with SNIPE_MAX_FEE_BPS=5."""
    assert passes_strategy_c_gate(fee_rate_bps=6, max_fee_bps=5) is False


def test_strategy_c_passes_zero_fee():
    assert passes_strategy_c_gate(fee_rate_bps=0, max_fee_bps=5) is True
