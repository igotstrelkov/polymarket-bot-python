"""
Unit tests for strategies/strategy_b.py.

Covers entry gates, budget cap, price range filter, size calculation,
and taker (non-Post-Only) order type.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import PriceLevel
from inventory.manager import InventoryState
from strategies.strategy_b import StrategyB


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_market(
    *,
    accepting_orders: bool = True,
    resolution_time: datetime | None = None,
    token_id: str = "tok1",
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond1",
        tick_size=0.001,
        minimum_order_size=1.0,
        neg_risk=False,
        fees_enabled=False,
        fee_rate_bps=0,
        seconds_delay=0,
        accepting_orders=accepting_orders,
        game_start_time=None,
        resolution_time=resolution_time,
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )


def make_book(ask: float = 0.01) -> BookStateStore:
    store = BookStateStore(token_id="tok1")
    store.bids = [PriceLevel(price=ask * 0.5, size=100)]
    store.asks = [PriceLevel(price=ask, size=100)]
    store.last_mid = ask * 0.75
    return store


def make_fee_cache(rate: int = 0) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = rate
    return cache


def make_inventory() -> InventoryState:
    return InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.5)


def far_resolution() -> datetime:
    """Returns a datetime 48 hours in the future."""
    return datetime.fromtimestamp(time.time() + 172_800, tz=timezone.utc)


# ── Kill switch / disabled ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_kill_switch():
    s = StrategyB(kill_switch_active=True)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_disabled():
    s = StrategyB(enabled=False)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate: accepting_orders ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_not_accepting_orders():
    s = StrategyB()
    result = await s.evaluate(
        make_market(accepting_orders=False, resolution_time=far_resolution()),
        make_book(), make_inventory(), make_fee_cache(),
    )
    assert result == []


# ── Budget cap ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_budget_exhausted():
    s = StrategyB(max_exposure=200.0, current_total_position=200.0)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_proceeds_when_position_below_budget():
    s = StrategyB(max_exposure=200.0, current_total_position=150.0)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()),
        make_book(ask=0.01), make_inventory(), make_fee_cache(),
    )
    assert len(result) == 1


# ── Gate: resolution >= 24h ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_resolution_within_24h():
    soon = datetime.fromtimestamp(time.time() + 3_600, tz=timezone.utc)  # 1h
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=soon), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_proceeds_when_resolution_beyond_24h():
    far = far_resolution()  # 48h
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far), make_book(ask=0.01), make_inventory(), make_fee_cache()
    )
    assert len(result) == 1


# ── Gate: price range ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_ask_above_penny_max():
    s = StrategyB(penny_max_price=0.03)
    book = make_book(ask=0.04)  # above max
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), book, make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_ask_below_penny_min():
    s = StrategyB(penny_min_price=0.001)
    book = make_book(ask=0.0005)  # below min
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), book, make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_signal_when_ask_at_penny_min():
    s = StrategyB(penny_min_price=0.001, penny_max_price=0.03)
    book = make_book(ask=0.001)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_returns_signal_when_ask_at_penny_max():
    s = StrategyB(penny_min_price=0.001, penny_max_price=0.03)
    book = make_book(ask=0.03)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1


# ── Signal properties ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_is_buy():
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.01),
        make_inventory(), make_fee_cache()
    )
    assert result[0].side == "BUY"


@pytest.mark.asyncio
async def test_signal_is_not_post_only():
    """Strategy B uses taker orders — not Post-Only."""
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.01),
        make_inventory(), make_fee_cache()
    )
    assert result[0].post_only is False


@pytest.mark.asyncio
async def test_signal_is_gtc():
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.01),
        make_inventory(), make_fee_cache()
    )
    assert result[0].time_in_force == "GTC"


@pytest.mark.asyncio
async def test_signal_price_is_best_ask():
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.015),
        make_inventory(), make_fee_cache()
    )
    assert result[0].price == pytest.approx(0.015)


@pytest.mark.asyncio
async def test_signal_strategy_label():
    s = StrategyB()
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.01),
        make_inventory(), make_fee_cache()
    )
    assert result[0].strategy == "B"


# ── Size calculation ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_size_is_budget_divided_by_price():
    """penny_budget=5.0, price=0.01 → size=int(500)=500."""
    s = StrategyB(penny_budget=5.0)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.01),
        make_inventory(), make_fee_cache()
    )
    assert result[0].size == 500


@pytest.mark.asyncio
async def test_size_minimum_is_1():
    """Even if budget < price per share, minimum size is 1."""
    s = StrategyB(penny_budget=0.001)
    result = await s.evaluate(
        make_market(resolution_time=far_resolution()), make_book(ask=0.03),
        make_inventory(), make_fee_cache()
    )
    assert result[0].size >= 1
