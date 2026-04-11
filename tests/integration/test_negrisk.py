"""
Integration tests for negRisk market handling.

- Strategy A: negRisk market → neg_risk=True in generated signals / EIP-712 payload
- Strategy C: negRisk market always excluded regardless of probability (§5.3.2)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from config.settings import Settings
from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import BookEvent, PriceLevel
from fees.cache import FeeRateCache
from inventory.manager import InventoryState
from strategies.strategy_a import StrategyA
from strategies.strategy_c import StrategyC


# ── Fixtures / helpers ─────────────────────────────────────────────────────────

def make_book(token_id: str = "tok_1", bid: float = 0.44, ask: float = 0.56) -> BookStateStore:
    book = BookStateStore(token_id=token_id)
    book.update(BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=bid, size=100.0)],
        asks=[PriceLevel(price=ask, size=100.0)],
        timestamp=time.time(),
    ))
    return book


def make_fee_cache(token_id: str = "tok_1", bps: int = 0) -> FeeRateCache:
    cache = FeeRateCache(ttl_s=60)
    cache.set(token_id, bps)
    return cache


def resolution_time_far() -> datetime:
    """Resolution time 10 hours from now — well outside all warning windows."""
    return datetime.now(timezone.utc) + timedelta(hours=10)


def resolution_time_snipe_window() -> datetime:
    """Resolution time 3 hours from now — inside Strategy C snipe window (2h–4h)."""
    return datetime.now(timezone.utc) + timedelta(hours=3)


def make_market(
    token_id: str = "tok_1",
    neg_risk: bool = False,
    accepting_orders: bool = True,
    resolution_time: datetime | None = None,
    tick_size: float = 0.01,
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond_1",
        tick_size=tick_size,
        minimum_order_size=5.0,
        neg_risk=neg_risk,
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


# ── Strategy A: negRisk=True in signal ───────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_a_negrisk_market_produces_signals():
    """Strategy A does not gate on neg_risk; it produces signals for negRisk markets."""
    strategy = StrategyA(
        base_spread=0.04,
        cost_floor=0.01,
        order_size=10,
    )
    market = make_market(neg_risk=True, resolution_time=resolution_time_far())
    book = make_book()
    inventory = InventoryState(yes_price=0.50)
    fee_cache = make_fee_cache()

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert len(signals) > 0, "Strategy A should produce signals for negRisk markets"


@pytest.mark.asyncio
async def test_strategy_a_signal_has_neg_risk_true():
    """Signal.neg_risk is True when market.neg_risk is True (flows into EIP-712)."""
    strategy = StrategyA(
        base_spread=0.04,
        cost_floor=0.01,
        order_size=10,
    )
    market = make_market(neg_risk=True, resolution_time=resolution_time_far())
    book = make_book()
    inventory = InventoryState(yes_price=0.50)
    fee_cache = make_fee_cache()

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert all(s.neg_risk is True for s in signals), (
        "All signals for a negRisk market must carry neg_risk=True"
    )


@pytest.mark.asyncio
async def test_strategy_a_non_negrisk_market_signal_has_neg_risk_false():
    """Signal.neg_risk is False when market.neg_risk is False."""
    strategy = StrategyA(base_spread=0.04, cost_floor=0.01, order_size=10)
    market = make_market(neg_risk=False, resolution_time=resolution_time_far())
    book = make_book()
    inventory = InventoryState(yes_price=0.50)
    fee_cache = make_fee_cache()

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert len(signals) > 0
    assert all(s.neg_risk is False for s in signals)


@pytest.mark.asyncio
async def test_strategy_a_negrisk_signal_contains_all_required_eip712_fields():
    """NegRisk signal contains all EIP-712 payload fields (neg_risk, fee_rate_bps, etc.)."""
    strategy = StrategyA(base_spread=0.04, cost_floor=0.01, order_size=10)
    market = make_market(neg_risk=True, resolution_time=resolution_time_far())
    book = make_book()
    inventory = InventoryState(yes_price=0.50)
    fee_cache = make_fee_cache(bps=78)

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    for sig in signals:
        assert sig.neg_risk is True
        assert sig.fee_rate_bps == 78
        assert sig.token_id == "tok_1"
        assert sig.post_only is True
        assert sig.strategy == "A"


@pytest.mark.asyncio
async def test_strategy_a_produces_bid_and_ask_for_negrisk():
    """Strategy A produces both BUY and SELL signals for negRisk market."""
    strategy = StrategyA(base_spread=0.04, cost_floor=0.01, order_size=10)
    market = make_market(neg_risk=True, resolution_time=resolution_time_far())
    book = make_book()
    inventory = InventoryState(yes_price=0.50)
    fee_cache = make_fee_cache()

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    sides = {s.side for s in signals}
    assert "BUY" in sides
    assert "SELL" in sides


# ── Strategy C: negRisk market always excluded ────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_c_negrisk_market_excluded():
    """Strategy C returns empty list for negRisk=True market (§5.3.2)."""
    strategy = StrategyC(
        prob_threshold=0.90,
        max_fee_bps=5,
        snipe_min_size=5,
        snipe_max_size=20,
    )
    # Use snipe window with high certainty market
    market = make_market(neg_risk=True, resolution_time=resolution_time_snipe_window())
    # High certainty book: mid ≈ 0.95 > prob_threshold
    book = make_book(bid=0.93, ask=0.97)
    inventory = InventoryState(yes_price=0.95)
    fee_cache = make_fee_cache(bps=0)  # passes fee gate

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert signals == [], (
        "Strategy C must exclude negRisk markets regardless of probability"
    )


@pytest.mark.asyncio
async def test_strategy_c_negrisk_excluded_even_at_maximum_certainty():
    """Strategy C excludes negRisk market even when mid is very close to 1.0."""
    strategy = StrategyC(prob_threshold=0.90, max_fee_bps=5)
    market = make_market(neg_risk=True, resolution_time=resolution_time_snipe_window())
    book = make_book(bid=0.98, ask=0.99)
    inventory = InventoryState(yes_price=0.985)
    fee_cache = make_fee_cache(bps=0)

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert signals == []


@pytest.mark.asyncio
async def test_strategy_c_non_negrisk_market_can_produce_signals():
    """Strategy C can produce signals for normal (neg_risk=False) market."""
    strategy = StrategyC(prob_threshold=0.90, max_fee_bps=5, snipe_min_size=5, snipe_max_size=20)
    market = make_market(neg_risk=False, resolution_time=resolution_time_snipe_window())
    # High certainty: mid≈0.95 > 0.90
    book = make_book(bid=0.93, ask=0.97)
    inventory = InventoryState(yes_price=0.95)
    fee_cache = make_fee_cache(bps=0)

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert len(signals) > 0


@pytest.mark.asyncio
async def test_strategy_c_neg_risk_flag_propagated_to_signal_for_normal_market():
    """For non-negRisk markets, signal.neg_risk=False (neg_risk flows into payload)."""
    strategy = StrategyC(prob_threshold=0.90, max_fee_bps=5, snipe_min_size=5, snipe_max_size=20)
    market = make_market(neg_risk=False, resolution_time=resolution_time_snipe_window())
    book = make_book(bid=0.93, ask=0.97)
    inventory = InventoryState(yes_price=0.95)
    fee_cache = make_fee_cache(bps=0)

    signals = await strategy.evaluate(market, book, inventory, fee_cache)

    assert len(signals) > 0
    assert all(s.neg_risk is False for s in signals)
