"""
Unit tests for core/execution/quote_engine.py.

Covers strategy aggregation, skip-disabled/kill-switched strategies,
reward constraint application (FR-402, FR-403), and OrderIntent output.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.quote_engine import QuoteEngine
from core.execution.types import OrderIntent, PriceLevel, Signal
from inventory.manager import InventoryState


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_market(
    *,
    token_id: str = "tok1",
    adjusted_midpoint: float | None = None,
    rewards_max_spread: float | None = None,
    rewards_min_size: float | None = None,
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond1",
        tick_size=0.01,
        minimum_order_size=1.0,
        neg_risk=False,
        fees_enabled=True,
        fee_rate_bps=78,
        seconds_delay=0,
        accepting_orders=True,
        game_start_time=None,
        resolution_time=None,
        rewards_min_size=rewards_min_size,
        rewards_max_spread=rewards_max_spread,
        rewards_daily_rate=None,
        adjusted_midpoint=adjusted_midpoint,
        tags=[],
    )


def make_book() -> BookStateStore:
    store = BookStateStore(token_id="tok1")
    store.bids = [PriceLevel(price=0.48, size=100)]
    store.asks = [PriceLevel(price=0.52, size=100)]
    store.last_mid = 0.50
    return store


def make_fee_cache() -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = 78
    return cache


def make_inventory() -> InventoryState:
    return InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.5)


def make_signal(
    *,
    side: str = "BUY",
    price: float = 0.49,
    size: float = 10,
    strategy: str = "A",
) -> Signal:
    return Signal(
        token_id="tok1",
        side=side,
        price=price,
        size=size,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy=strategy,
        fee_rate_bps=78,
        neg_risk=False,
        tick_size=0.01,
    )


def mock_strategy(
    *,
    enabled: bool = True,
    kill_switch_active: bool = False,
    signals: list[Signal] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.enabled = enabled
    s.kill_switch_active = kill_switch_active
    s.evaluate = AsyncMock(return_value=signals or [])
    return s


# ── Strategy aggregation ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_calls_all_enabled_strategies():
    sig_a = make_signal(strategy="A")
    sig_b = make_signal(strategy="B")
    strat_a = mock_strategy(signals=[sig_a])
    strat_b = mock_strategy(signals=[sig_b])

    engine = QuoteEngine(strategies=[strat_a, strat_b])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())

    strat_a.evaluate.assert_awaited_once()
    strat_b.evaluate.assert_awaited_once()
    assert len(result) == 2


@pytest.mark.asyncio
async def test_compute_skips_disabled_strategy():
    sig = make_signal()
    enabled = mock_strategy(enabled=True, signals=[sig])
    disabled = mock_strategy(enabled=False, signals=[sig])

    engine = QuoteEngine(strategies=[enabled, disabled])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())

    disabled.evaluate.assert_not_awaited()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_compute_skips_kill_switched_strategy():
    sig = make_signal()
    active = mock_strategy(enabled=True, kill_switch_active=False, signals=[sig])
    killed = mock_strategy(enabled=True, kill_switch_active=True, signals=[sig])

    engine = QuoteEngine(strategies=[active, killed])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())

    killed.evaluate.assert_not_awaited()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_compute_returns_empty_when_no_strategies():
    engine = QuoteEngine(strategies=[])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())
    assert result == []


@pytest.mark.asyncio
async def test_compute_returns_order_intents():
    sig = make_signal()
    strat = mock_strategy(signals=[sig])
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())
    assert len(result) == 1
    assert isinstance(result[0], OrderIntent)


# ── Signal → OrderIntent field mapping ───────────────────────────────────────

@pytest.mark.asyncio
async def test_order_intent_fields_match_signal():
    sig = make_signal(side="SELL", price=0.51, size=15)
    strat = mock_strategy(signals=[sig])
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(make_market(), make_book(), make_inventory(), make_fee_cache())

    intent = result[0]
    assert intent.side == "SELL"
    assert intent.price == pytest.approx(0.51)
    assert intent.size == pytest.approx(15)
    assert intent.post_only is True
    assert intent.fee_rate_bps == 78
    assert intent.strategy == "A"


# ── Reward constraints (FR-402, FR-403) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fr402_price_clamped_within_rewards_max_spread():
    """Price outside rewardsMaxSpread/2 of adjustedMidpoint is clamped."""
    # adjustedMidpoint=0.50, rewardsMaxSpread=0.04 → valid range [0.48, 0.52]
    # Signal price=0.40 (below lower bound) → should be clamped to 0.48
    sig = make_signal(side="BUY", price=0.40)
    strat = mock_strategy(signals=[sig])
    market = make_market(adjusted_midpoint=0.50, rewards_max_spread=0.04)
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].price == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_fr402_price_unchanged_when_within_rewards_spread():
    """Price within range is not modified."""
    sig = make_signal(side="BUY", price=0.49)
    strat = mock_strategy(signals=[sig])
    market = make_market(adjusted_midpoint=0.50, rewards_max_spread=0.04)
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].price == pytest.approx(0.49)


@pytest.mark.asyncio
async def test_fr402_no_constraint_when_no_adjusted_midpoint():
    """If adjusted_midpoint is None, reward constraints are not applied."""
    sig = make_signal(side="BUY", price=0.30)
    strat = mock_strategy(signals=[sig])
    market = make_market(adjusted_midpoint=None, rewards_max_spread=0.04)
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].price == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_fr403_size_raised_to_rewards_min_size():
    """Signal size below rewardsMinSize is bumped up (FR-403)."""
    sig = make_signal(size=5)
    strat = mock_strategy(signals=[sig])
    market = make_market(
        adjusted_midpoint=0.50, rewards_max_spread=0.10, rewards_min_size=15.0
    )
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].size >= 15.0


@pytest.mark.asyncio
async def test_fr403_size_unchanged_when_already_meets_min():
    """Signal size above rewardsMinSize is not changed."""
    sig = make_signal(size=20)
    strat = mock_strategy(signals=[sig])
    market = make_market(
        adjusted_midpoint=0.50, rewards_max_spread=0.10, rewards_min_size=10.0
    )
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].size == pytest.approx(20)


@pytest.mark.asyncio
async def test_sell_signal_price_clamped_to_rewards_range():
    """SELL signal with price outside upper bound is clamped down."""
    # adjustedMidpoint=0.50, rewardsMaxSpread=0.04 → range [0.48, 0.52]
    sig = make_signal(side="SELL", price=0.60)  # above upper bound
    strat = mock_strategy(signals=[sig])
    market = make_market(adjusted_midpoint=0.50, rewards_max_spread=0.04)
    engine = QuoteEngine(strategies=[strat])
    result = await engine.compute(market, make_book(), make_inventory(), make_fee_cache())
    assert result[0].price == pytest.approx(0.52)
