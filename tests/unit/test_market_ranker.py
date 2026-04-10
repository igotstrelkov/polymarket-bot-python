"""
Unit tests for core/control/market_ranker.py.

Covers: EV term computation, cold-start fallbacks, EV ≤ 0 exclusion,
MM_MAX_MARKETS cap, capital allocation (pro-rata, MAX_PER_MARKET,
MM_MIN_ORDER_SIZE floor), and event_risk_cost decay.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.control.market_ranker import (
    MarketEVInputs,
    _cold_start_fill_prob,
    rank,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    base = dict(
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="pass",
        MM_MAX_MARKETS=20,
        MM_MIN_ORDER_SIZE=0,
        MAX_PER_MARKET=100.0,
        MAX_TOTAL_EXPOSURE=2000.0,
    )
    base.update(overrides)
    return Settings(**base)


def make_inputs(
    token_id: str = "tok1",
    *,
    observed_half_spread: float = 0.03,
    tick_size: float = 0.01,
    fill_count: int = 200,
    hours_live: float = 48.0,
    fill_probability: float | None = 0.30,
    adverse_selection_cost: float | None = 0.005,
    rewards_daily_rate: float | None = None,
    proximity_score: float = 0.0,
    rebate_rate_decimal: float = 0.0,
    expected_daily_volume: float = 0.0,
    inventory_skew: float = 0.0,
    time_to_resolution_h: float | None = None,
    posted_ticks_from_mid: float = 1.0,
) -> MarketEVInputs:
    return MarketEVInputs(
        token_id=token_id,
        tick_size=tick_size,
        observed_half_spread=observed_half_spread,
        posted_ticks_from_mid=posted_ticks_from_mid,
        fill_count=fill_count,
        hours_live=hours_live,
        fill_probability=fill_probability,
        adverse_selection_cost=adverse_selection_cost,
        rewards_daily_rate=rewards_daily_rate,
        proximity_score=proximity_score,
        rebate_rate_decimal=rebate_rate_decimal,
        expected_daily_volume=expected_daily_volume,
        inventory_skew=inventory_skew,
        time_to_resolution_h=time_to_resolution_h,
    )


# ── Cold-start fill probability curve ────────────────────────────────────────

def test_cold_start_fill_prob_at_mid():
    assert _cold_start_fill_prob(0) == pytest.approx(0.5)


def test_cold_start_fill_prob_at_5_ticks():
    assert _cold_start_fill_prob(5) == pytest.approx(0.05)


def test_cold_start_fill_prob_at_2_ticks():
    expected = 0.5 - (2 / 5) * (0.5 - 0.05)
    assert _cold_start_fill_prob(2) == pytest.approx(expected)


def test_cold_start_fill_prob_clamped_beyond_5_ticks():
    assert _cold_start_fill_prob(10) == pytest.approx(0.05)


# ── Cold-start detection ──────────────────────────────────────────────────────

def test_cold_start_when_fill_count_below_100():
    inputs = make_inputs(fill_count=50, hours_live=48.0)
    result = rank([inputs], make_settings())
    assert result[0].is_cold_start is True


def test_cold_start_when_hours_live_below_24():
    inputs = make_inputs(fill_count=200, hours_live=12.0)
    result = rank([inputs], make_settings())
    assert result[0].is_cold_start is True


def test_not_cold_start_when_both_thresholds_met():
    inputs = make_inputs(fill_count=100, hours_live=24.0)
    result = rank([inputs], make_settings())
    assert result[0].is_cold_start is False


def test_cold_start_uses_fixed_fill_prob_and_1_tick_adverse():
    """In cold-start, adverse_selection_cost = tick_size regardless of input."""
    inputs = make_inputs(
        fill_count=10,
        hours_live=1.0,
        tick_size=0.01,
        adverse_selection_cost=0.001,   # would be much lower, but cold-start overrides
        fill_probability=0.99,           # would be very high, but cold-start overrides
        observed_half_spread=0.05,
    )
    result = rank([inputs], make_settings())
    assert result[0].adverse_selection_cost == pytest.approx(0.01)  # 1 tick


# ── EV term correctness ───────────────────────────────────────────────────────

def test_spread_ev_equals_half_spread_times_fill_prob():
    inputs = make_inputs(
        observed_half_spread=0.04,
        fill_probability=0.25,
    )
    result = rank([inputs], make_settings())
    assert result[0].spread_ev == pytest.approx(0.04 * 0.25)


def test_reward_ev_zero_when_no_rate():
    inputs = make_inputs(rewards_daily_rate=None)
    result = rank([inputs], make_settings())
    assert result[0].reward_ev == pytest.approx(0.0)


def test_reward_ev_equals_rate_times_proximity():
    inputs = make_inputs(rewards_daily_rate=10.0, proximity_score=0.05)
    result = rank([inputs], make_settings())
    assert result[0].reward_ev == pytest.approx(0.5)


def test_rebate_ev_equals_rate_times_volume():
    inputs = make_inputs(rebate_rate_decimal=0.0078, expected_daily_volume=100.0)
    result = rank([inputs], make_settings())
    assert result[0].rebate_ev == pytest.approx(0.78)


def test_inventory_cost_scales_with_abs_skew():
    # Use large spread and low adverse cost so EV stays positive after inventory cost
    inputs_pos = make_inputs(
        inventory_skew=0.5, tick_size=0.01,
        observed_half_spread=0.20, fill_probability=0.5,
        adverse_selection_cost=0.001,
    )
    inputs_neg = make_inputs(
        inventory_skew=-0.5, tick_size=0.01,
        observed_half_spread=0.20, fill_probability=0.5,
        adverse_selection_cost=0.001,
    )
    r_pos = rank([inputs_pos], make_settings())[0]
    r_neg = rank([inputs_neg], make_settings())[0]
    assert r_pos.inventory_cost == pytest.approx(0.005)
    assert r_neg.inventory_cost == pytest.approx(0.005)


def test_event_risk_cost_zero_beyond_6h():
    inputs = make_inputs(time_to_resolution_h=7.0, observed_half_spread=0.03)
    result = rank([inputs], make_settings())
    assert result[0].event_risk_cost == pytest.approx(0.0)


def test_event_risk_cost_nonzero_within_6h():
    """Market 3h from resolution → event_risk_cost = 0.5 × half_spread."""
    # fill_prob=0.8 > fraction=0.5, so spread_ev outweighs event_risk_cost → EV positive
    inputs = make_inputs(
        time_to_resolution_h=3.0,
        observed_half_spread=0.20,
        fill_probability=0.8,
        adverse_selection_cost=0.001,
    )
    result = rank([inputs], make_settings())
    # fraction = 1 - 3/6 = 0.5; cost = 0.5 × 0.20 = 0.10
    assert result[0].event_risk_cost == pytest.approx(0.10)


def test_event_risk_cost_zero_when_resolution_none():
    inputs = make_inputs(time_to_resolution_h=None)
    result = rank([inputs], make_settings())
    assert result[0].event_risk_cost == pytest.approx(0.0)


# ── EV ≤ 0 exclusion ─────────────────────────────────────────────────────────

def test_market_with_negative_ev_excluded():
    """Very high adverse_selection_cost drives EV negative → excluded."""
    inputs = make_inputs(adverse_selection_cost=1.0)  # 1 USDC adverse
    result = rank([inputs], make_settings())
    assert result == []


def test_market_with_zero_ev_excluded():
    """EV exactly 0 → excluded (must be strictly > 0)."""
    # Craft EV = 0: half_spread=0.01 * fill_prob=1.0 - adverse=0.01 = 0
    inputs = make_inputs(
        observed_half_spread=0.01,
        fill_probability=1.0,
        adverse_selection_cost=0.01,
        tick_size=0.001,  # tiny so inventory_cost ≈ 0
    )
    result = rank([inputs], make_settings())
    assert result == []


# ── MM_MAX_MARKETS cap ────────────────────────────────────────────────────────

def test_rank_caps_at_max_markets():
    markets = [
        make_inputs(token_id=f"t{i}", observed_half_spread=0.05)
        for i in range(30)
    ]
    settings = make_settings(MM_MAX_MARKETS=10)
    result = rank(markets, settings)
    assert len(result) <= 10


def test_rank_selects_highest_ev_first():
    good = make_inputs("good", observed_half_spread=0.10)
    bad  = make_inputs("bad",  observed_half_spread=0.01)
    settings = make_settings(MM_MAX_MARKETS=1)
    result = rank([bad, good], settings)
    assert len(result) == 1
    assert result[0].token_id == "good"


# ── Capital allocation ────────────────────────────────────────────────────────

def test_allocation_does_not_exceed_max_per_market():
    markets = [make_inputs(f"t{i}") for i in range(5)]
    settings = make_settings(MAX_PER_MARKET=50.0, MAX_TOTAL_EXPOSURE=2000.0)
    result = rank(markets, settings)
    for r in result:
        assert r.allocated_exposure <= 50.0 + 1e-9


def test_allocation_sum_does_not_exceed_total_exposure():
    markets = [make_inputs(f"t{i}") for i in range(10)]
    settings = make_settings(MAX_PER_MARKET=500.0, MAX_TOTAL_EXPOSURE=1000.0)
    result = rank(markets, settings)
    assert sum(r.allocated_exposure for r in result) <= 1000.0 + 1e-6


def test_allocation_min_floor_respected():
    """Each selected market gets ≥ MM_MIN_ORDER_SIZE in its allocation."""
    markets = [make_inputs(f"t{i}") for i in range(3)]
    settings = make_settings(MM_MIN_ORDER_SIZE=10, MAX_PER_MARKET=500.0, MAX_TOTAL_EXPOSURE=2000.0)
    result = rank(markets, settings)
    for r in result:
        assert r.allocated_exposure >= 10.0 - 1e-9


def test_allocation_single_market_gets_min_of_cap_and_budget():
    """Single market with large EV → allocation ≤ MAX_PER_MARKET."""
    inputs = make_inputs("t1", observed_half_spread=0.10)
    settings = make_settings(MAX_PER_MARKET=80.0, MAX_TOTAL_EXPOSURE=2000.0)
    result = rank([inputs], settings)
    assert result[0].allocated_exposure <= 80.0 + 1e-9


def test_empty_input_returns_empty():
    assert rank([], make_settings()) == []
