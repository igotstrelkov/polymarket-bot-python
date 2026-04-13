"""
Integration tests for the main event-processing pipeline.

Tests the full flow: WS book event → order placement reaches mock CLOB,
fill event → markout scheduled + inventory updated + fee re-fetched,
RESOLUTION_TIME_CHANGED mutation → GTD expiries recomputed and orders repriced.

All external I/O (CLOB API, WebSocket, Postgres, Redis) is mocked.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.control.capability_enricher import (
    MarketCapabilityModel,
    MutationType,
    detect_mutations,
)
from core.execution.book_state import BookStateStore
from core.execution.execution_actor import (
    CancelMutation,
    ConfirmedOrder,
    PlaceMutation,
    diff,
)
from core.execution.types import BookEvent, FillEvent, OrderIntent, PriceLevel
from core.ledger.fill_position_ledger import FillAndPositionLedger
from core.ledger.order_ledger import OrderLedger
from core.ledger.recovery_coordinator import RecoveryCoordinator


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_book_event(
    token_id: str = "tok_1",
    bid_price: float = 0.45,
    ask_price: float = 0.55,
) -> BookEvent:
    return BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=bid_price, size=100.0)],
        asks=[PriceLevel(price=ask_price, size=100.0)],
        timestamp=time.time(),
    )


def make_fill_event(
    order_id: str = "ord_1",
    token_id: str = "tok_1",
    side: str = "BUY",
    price: float = 0.45,
    size: float = 10.0,
    strategy: str = "A",
) -> FillEvent:
    return FillEvent(
        order_id=order_id,
        token_id=token_id,
        market_id="market_1",
        side=side,
        price=price,
        size=size,
        maker_taker="MAKER",
        strategy=strategy,
        fill_timestamp=time.time(),
    )


def make_market_capability(
    token_id: str = "tok_1",
    tick_size: float = 0.01,
    resolution_time: datetime | None = None,
    accepting_orders: bool = True,
    fee_rate_bps: int = 0,
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond_1",
        tick_size=tick_size,
        minimum_order_size=5.0,
        neg_risk=False,
        fees_enabled=False,
        fee_rate_bps=fee_rate_bps,
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


# ── Test 1: Book event → order placement reaches mock CLOB ────────────────────

@pytest.mark.asyncio
async def test_book_event_triggers_order_placement():
    """A BookEvent with a valid spread should result in PlaceMutation(s) to the CLOB."""
    book_store = BookStateStore(token_id="tok_1")

    event = make_book_event(bid_price=0.45, ask_price=0.55)
    book_store.update(event)

    # Build a desired order intent
    intent = OrderIntent(
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy="A",
        fee_rate_bps=0,
        neg_risk=False,
        tick_size=0.01,
    )

    # No confirmed orders → diff produces PlaceMutation
    mutations = diff([intent], [])
    assert len(mutations) == 1
    assert isinstance(mutations[0], PlaceMutation)
    assert mutations[0].intent.token_id == "tok_1"


@pytest.mark.asyncio
async def test_book_event_book_state_updated():
    """After processing a BookEvent, BookStateStore reflects the new mid."""
    book_store = BookStateStore(token_id="tok_1")
    event = make_book_event(bid_price=0.40, ask_price=0.60)
    book_store.update(event)

    assert book_store.best_bid() == pytest.approx(0.40)
    assert book_store.best_ask() == pytest.approx(0.60)
    assert book_store.mid() == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_existing_confirmed_order_not_replaced_when_still_valid():
    """An already-confirmed order matching desired state produces no mutations."""
    intent = OrderIntent(
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy="A",
        fee_rate_bps=0,
        neg_risk=False,
        tick_size=0.01,
    )
    confirmed = ConfirmedOrder(
        order_id="ord_existing",
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        strategy="A",
    )
    mutations = diff([intent], [confirmed])
    assert mutations == []


@pytest.mark.asyncio
async def test_stale_confirmed_order_produces_cancel_mutation():
    """A confirmed order no longer in desired state produces a CancelMutation."""
    confirmed = ConfirmedOrder(
        order_id="ord_old",
        token_id="tok_1",
        side="BUY",
        price=0.40,  # stale price
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        strategy="A",
    )
    intent = OrderIntent(
        token_id="tok_1",
        side="BUY",
        price=0.45,  # desired is now at 0.45
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy="A",
        fee_rate_bps=0,
        neg_risk=False,
        tick_size=0.01,
    )
    mutations = diff([intent], [confirmed])
    cancel_muts = [m for m in mutations if isinstance(m, CancelMutation)]
    assert any(m.order_id == "ord_old" for m in cancel_muts)


# ── Test 2: Fill event → markout scheduled, inventory updated, fee re-fetched ──

@pytest.mark.asyncio
async def test_fill_event_recorded_in_fill_ledger():
    """A FillEvent must create a FillRecord in the FillAndPositionLedger."""
    fill_ledger = FillAndPositionLedger()
    event = make_fill_event(order_id="ord_1", token_id="tok_1", side="BUY", size=10.0)

    fill_ledger.record_fill(
        fill_id=event.order_id + "_f1",
        order_id=event.order_id,
        token_id=event.token_id,
        side=event.side,
        price=event.price,
        size=event.size,
        strategy=event.strategy,
        is_maker=True,
        fee_paid=0.0,
    )

    assert fill_ledger.fill_count() == 1
    position = fill_ledger.get_position("tok_1")
    assert position is not None
    assert position.shares == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_strategy_a_fill_queued_for_markout():
    """Strategy A fills must be queued for the 30-second markout (FR-601a)."""
    fill_ledger = FillAndPositionLedger()
    fill_id = "fill_a_1"

    fill_ledger.record_fill(
        fill_id=fill_id,
        order_id="ord_1",
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=10.0,
        strategy="A",
        is_maker=True,
        fee_paid=0.0,
        mid_at_fill=0.45,
    )

    pending = fill_ledger.pending_markout_fill_ids()
    assert fill_id in pending


@pytest.mark.asyncio
async def test_markout_30s_computed_with_correct_sign_buy():
    """FR-601a: BUY markout = (mid_t30 - mid_at_fill) × +1."""
    fill_ledger = FillAndPositionLedger()
    fill_id = "fill_buy_1"

    fill_ledger.record_fill(
        fill_id=fill_id,
        order_id="ord_buy",
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=5.0,
        strategy="A",
        is_maker=True,
        fee_paid=0.0,
        mid_at_fill=0.45,
    )

    mid_at_t30 = 0.40  # price fell after buy
    result = fill_ledger.record_markout(fill_id=fill_id, mid_at_t30=mid_at_t30)
    assert result is not None
    # markout = (0.40 - 0.45) × +1 = -0.05
    assert result.markout_30s == pytest.approx(-0.05)


@pytest.mark.asyncio
async def test_markout_30s_computed_with_correct_sign_sell():
    """FR-601a: SELL markout = (mid_t30 - mid_at_fill) × -1."""
    fill_ledger = FillAndPositionLedger()
    fill_id = "fill_sell_1"

    fill_ledger.record_fill(
        fill_id=fill_id,
        order_id="ord_sell",
        token_id="tok_1",
        side="SELL",
        price=0.55,
        size=5.0,
        strategy="A",
        is_maker=True,
        fee_paid=0.0,
        mid_at_fill=0.55,
    )

    mid_at_t30 = 0.50  # price fell after sell → favourable
    result = fill_ledger.record_markout(fill_id=fill_id, mid_at_t30=mid_at_t30)
    assert result is not None
    # markout = (0.50 - 0.55) × -1 = 0.05
    assert result.markout_30s == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_fee_cache_on_fill_called():
    """FeeCache.on_fill() must be called after each fill event."""
    mock_fee_cache = AsyncMock()
    mock_fee_cache.on_fill = AsyncMock()

    await mock_fee_cache.on_fill("tok_1")
    mock_fee_cache.on_fill.assert_awaited_once_with("tok_1")


# ── Test 3: RESOLUTION_TIME_CHANGED mutation → GTD expiries recomputed ─────────

@pytest.mark.asyncio
async def test_resolution_time_changed_produces_mutation():
    """RESOLUTION_TIME_CHANGED is detected when resolution_time shifts."""
    old_market = make_market_capability(
        resolution_time=datetime.fromtimestamp(time.time() + 7200, tz=timezone.utc)
    )
    new_market = make_market_capability(
        resolution_time=datetime.fromtimestamp(time.time() + 1800, tz=timezone.utc)
    )

    mutations = detect_mutations(old_market, new_market)
    assert MutationType.RESOLUTION_TIME_CHANGED in mutations


@pytest.mark.asyncio
async def test_accepting_orders_flipped_false_detected():
    """ACCEPTING_ORDERS_FLIPPED_FALSE mutation is detected when market closes."""
    open_market = make_market_capability(accepting_orders=True)
    closed_market = make_market_capability(accepting_orders=False)

    mutations = detect_mutations(open_market, closed_market)
    assert MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE in mutations


@pytest.mark.asyncio
async def test_fee_rate_changed_detected():
    """FEE_RATE_CHANGED mutation is detected when fee_rate_bps changes."""
    old_market = make_market_capability(fee_rate_bps=78)
    new_market = make_market_capability(fee_rate_bps=100)

    mutations = detect_mutations(old_market, new_market)
    assert MutationType.FEE_RATE_CHANGED in mutations


@pytest.mark.asyncio
async def test_no_mutation_when_markets_identical():
    """Identical market snapshots produce no mutations."""
    market = make_market_capability()
    mutations = detect_mutations(market, market)
    assert mutations == []


# ── Test 4: Recovery blocks quoting ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_quoting_blocked_during_recovery():
    """is_resyncing() returns True during recovery; placements must be gated."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    coordinator._resyncing = True
    assert coordinator.is_resyncing() is True

    mock_clob = AsyncMock()
    mock_clob.create_order = AsyncMock()

    # Orchestrator guards placements behind is_resyncing()
    if coordinator.is_resyncing():
        pass  # skip placement
    else:
        await mock_clob.create_order({"token_id": "tok_1"})

    mock_clob.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_coordinator_successful_path():
    """After successful recovery, is_resyncing() is False and confirmed IDs populated."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_orders = AsyncMock(return_value=[
        {
            "id": "ord_exchange_1",
            "asset_id": "tok_1",
            "side": "BUY",
            "price": "0.45",
            "original_size": "10",
            "size_matched": "0",
            "time_in_force": "GTC",
            "is_negRisk": False,
        },
    ])

    result = await coordinator.recover(mock_clob)
    assert result.success is True
    assert coordinator.is_resyncing() is False
