"""
Unit tests for core/execution/execution_actor.py.

Covers:
- diff(): minimum mutation set (place, cancel, no-op for unchanged)
- diff(): self-cross detection (FR-210a)
- diff(): replace = cancel + place when price changes
- ExecutionActor.apply(): DRY_RUN logging
- ExecutionActor.apply(): retry policy (10ms/25ms/50ms → reconciliation)
- ExecutionActor: confirm-cancel mode activation/deactivation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from config.settings import Settings
from core.execution.execution_actor import (
    CancelMutation,
    ConfirmedOrder,
    ExecutionActor,
    PlaceMutation,
    diff,
)
from core.execution.types import OrderIntent


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    base = dict(
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="pass",
        DRY_RUN=True,
        CANCEL_CONFIRM_THRESHOLD_PCT=5.0,
    )
    base.update(overrides)
    return Settings(**base)


def make_intent(
    *,
    token_id: str = "tok1",
    side: str = "BUY",
    price: float = 0.49,
    size: float = 10.0,
    tick_size: float = 0.01,
    time_in_force: str = "GTC",
    post_only: bool = True,
) -> OrderIntent:
    return OrderIntent(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        time_in_force=time_in_force,
        post_only=post_only,
        expiration=None,
        strategy="A",
        fee_rate_bps=78,
        neg_risk=False,
        tick_size=tick_size,
    )


def make_confirmed(
    *,
    order_id: str = "oid1",
    token_id: str = "tok1",
    side: str = "BUY",
    price: float = 0.49,
    size: float = 10.0,
    time_in_force: str = "GTC",
    post_only: bool = True,
) -> ConfirmedOrder:
    return ConfirmedOrder(
        order_id=order_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        time_in_force=time_in_force,
        post_only=post_only,
        strategy="A",
    )


# ── diff: basic cases ─────────────────────────────────────────────────────────

def test_diff_empty_desired_empty_confirmed():
    assert diff([], []) == []


def test_diff_new_intent_produces_place():
    """No confirmed order → PlaceMutation."""
    mutations = diff([make_intent()], [])
    places = [m for m in mutations if isinstance(m, PlaceMutation)]
    assert len(places) == 1
    assert places[0].intent.side == "BUY"


def test_diff_confirmed_not_in_desired_produces_cancel():
    """Confirmed order with no matching desired intent → CancelMutation."""
    mutations = diff([], [make_confirmed(order_id="oid1")])
    cancels = [m for m in mutations if isinstance(m, CancelMutation)]
    assert len(cancels) == 1
    assert cancels[0].order_id == "oid1"


def test_diff_matching_intent_and_confirmed_produces_no_mutation():
    """Exact match (same side, price, size, tif, post_only) → no mutation."""
    intent = make_intent(price=0.49, size=10.0)
    confirmed = make_confirmed(price=0.49, size=10.0)
    mutations = diff([intent], [confirmed])
    assert mutations == []


def test_diff_price_change_produces_cancel_then_place():
    """Intent at different price → CancelMutation for old + PlaceMutation for new."""
    intent = make_intent(price=0.50)           # desired at 0.50
    confirmed = make_confirmed(price=0.48)     # confirmed at 0.48
    mutations = diff([intent], [confirmed])
    cancels = [m for m in mutations if isinstance(m, CancelMutation)]
    places  = [m for m in mutations if isinstance(m, PlaceMutation)]
    assert len(cancels) == 1
    assert len(places) == 1
    assert cancels[0].order_id == confirmed.order_id


def test_diff_size_change_produces_cancel_then_place():
    intent = make_intent(size=15.0)
    confirmed = make_confirmed(size=10.0)
    mutations = diff([intent], [confirmed])
    assert any(isinstance(m, CancelMutation) for m in mutations)
    assert any(isinstance(m, PlaceMutation) for m in mutations)


def test_diff_cancels_before_places():
    """CancelMutations must appear before PlaceMutations in the output."""
    intent = make_intent(price=0.51)
    confirmed = make_confirmed(price=0.49, order_id="old")
    mutations = diff([intent], [confirmed])
    first_place = next((i for i, m in enumerate(mutations) if isinstance(m, PlaceMutation)), None)
    first_cancel = next((i for i, m in enumerate(mutations) if isinstance(m, CancelMutation)), None)
    assert first_cancel is not None
    assert first_place is not None
    assert first_cancel < first_place


def test_diff_multiple_intents_matched_correctly():
    """BUY and SELL both match their respective confirmed orders → no mutations."""
    buy_intent = make_intent(side="BUY",  price=0.49, size=10)
    sell_intent = make_intent(side="SELL", price=0.51, size=10)
    buy_confirmed  = make_confirmed(order_id="b1", side="BUY",  price=0.49, size=10)
    sell_confirmed = make_confirmed(order_id="s1", side="SELL", price=0.51, size=10)
    mutations = diff([buy_intent, sell_intent], [buy_confirmed, sell_confirmed])
    assert mutations == []


# ── diff: self-cross detection (FR-210a) ──────────────────────────────────────

def test_diff_self_cross_buy_within_tick_of_sell():
    """New BUY at 0.51 with confirmed SELL at 0.51 → cancel SELL + place BUY.

    The SELL at 0.51 is not in desired, so it is cancelled (no_longer_desired or
    self_cross — both prevent the cross). The important invariant is that it IS
    cancelled before the BUY is placed.
    """
    intent    = make_intent(side="BUY",  price=0.51, tick_size=0.01)
    confirmed = make_confirmed(order_id="sell1", side="SELL", price=0.51)
    mutations = diff([intent], [confirmed])
    cancels = [m for m in mutations if isinstance(m, CancelMutation)]
    places  = [m for m in mutations if isinstance(m, PlaceMutation)]
    assert len(cancels) == 1
    assert cancels[0].order_id == "sell1"
    assert len(places) == 1
    assert places[0].intent.side == "BUY"


def test_diff_self_cross_sell_within_tick_of_buy():
    """New SELL at 0.49 with confirmed BUY at 0.49 → cancel BUY before placing SELL."""
    intent    = make_intent(side="SELL", price=0.49, tick_size=0.01)
    confirmed = make_confirmed(order_id="buy1", side="BUY", price=0.49)
    mutations = diff([intent], [confirmed])
    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert "buy1" in cancel_ids


def test_diff_no_self_cross_when_spread_wide():
    """BUY at 0.46 vs SELL at 0.56 (10 ticks apart) → no self-cross cancel."""
    intent    = make_intent(side="BUY",  price=0.46, tick_size=0.01)
    confirmed = make_confirmed(order_id="sell1", side="SELL", price=0.56)
    mutations = diff([intent], [confirmed])
    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert self_cross_cancels == []


def test_diff_self_cross_not_double_cancelled():
    """A confirmed order already being cancelled is not also cancelled for self-cross."""
    # Confirmed SELL at 0.51 is both "no_longer_desired" AND within 1 tick of new BUY at 0.51
    # It should appear in cancels only once.
    intent    = make_intent(side="BUY", price=0.51, tick_size=0.01)
    confirmed = make_confirmed(order_id="sell1", side="SELL", price=0.51)
    # desired has only the BUY intent; the SELL is not in desired → "no_longer_desired"
    mutations = diff([intent], [confirmed])
    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert cancel_ids.count("sell1") == 1


# ── ExecutionActor.apply: DRY_RUN ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_dry_run_logs_without_api_call():
    settings = make_settings(DRY_RUN=True)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()

    mutations = [PlaceMutation(intent=make_intent())]
    result = await actor.apply(mutations, clob)

    clob.place_order.assert_not_awaited()
    assert len(result["placed"]) == 1


@pytest.mark.asyncio
async def test_apply_dry_run_cancel_logs_without_api_call():
    settings = make_settings(DRY_RUN=True)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()

    mutations = [CancelMutation(order_id="oid1", token_id="tok1")]
    result = await actor.apply(mutations, clob)

    clob.cancel_orders.assert_not_awaited()
    assert "oid1" in result["cancelled"]


# ── ExecutionActor.apply: live mode ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_live_places_order():
    settings = make_settings(DRY_RUN=False)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()
    clob.place_order = AsyncMock(return_value="order_abc")

    mutations = [PlaceMutation(intent=make_intent())]
    result = await actor.apply(mutations, clob)

    clob.place_order.assert_awaited_once()
    assert "order_abc" in result["placed"]


@pytest.mark.asyncio
async def test_apply_live_fire_and_forget_cancel():
    """In fire-and-forget mode, cancel is dispatched but placement proceeds immediately."""
    settings = make_settings(DRY_RUN=False)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()
    clob.cancel_orders = AsyncMock()
    clob.place_order = AsyncMock(return_value="placed_id")

    mutations = [
        CancelMutation(order_id="c1", token_id="tok1"),
        PlaceMutation(intent=make_intent()),
    ]
    result = await actor.apply(mutations, clob)

    assert "placed_id" in result["placed"]
    assert "c1" in result["cancelled"]


# ── Retry policy ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_on_duplicate_id_succeeds_on_second_attempt():
    """First attempt raises duplicate error; second attempt succeeds."""
    settings = make_settings(DRY_RUN=False)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()

    call_count = 0
    async def place_order_side_effect(_intent):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("duplicate order ID")
        return "retry_success"

    clob.place_order = AsyncMock(side_effect=place_order_side_effect)

    with patch("core.execution.execution_actor.asyncio.sleep", new=AsyncMock()):
        mutations = [PlaceMutation(intent=make_intent())]
        result = await actor.apply(mutations, clob)

    assert "retry_success" in result["placed"]
    assert call_count == 2


@pytest.mark.asyncio
async def test_retry_exhausted_returns_none_and_skips_market():
    """All 3 retry attempts fail → order not placed (force reconciliation signalled)."""
    settings = make_settings(DRY_RUN=False)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()
    clob.place_order = AsyncMock(side_effect=Exception("duplicate order ID"))

    with patch("core.execution.execution_actor.asyncio.sleep", new=AsyncMock()):
        mutations = [PlaceMutation(intent=make_intent())]
        result = await actor.apply(mutations, clob)

    # 3 retries exhausted → order not placed
    assert result["placed"] == []
    assert clob.place_order.await_count == 3  # 3 attempts


@pytest.mark.asyncio
async def test_non_duplicate_error_does_not_retry():
    """Non-duplicate errors abort immediately without retry."""
    settings = make_settings(DRY_RUN=False)
    actor = ExecutionActor(settings=settings)
    clob = AsyncMock()
    clob.place_order = AsyncMock(side_effect=Exception("internal server error"))

    mutations = [PlaceMutation(intent=make_intent())]
    result = await actor.apply(mutations, clob)

    # No retry for non-duplicate errors
    assert clob.place_order.await_count == 1
    assert result["placed"] == []


# ── Adaptive confirm-cancel mode ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_cancel_mode_activates_above_threshold():
    """Rejection rate > CANCEL_CONFIRM_THRESHOLD_PCT → confirm-cancel mode activates."""
    settings = make_settings(DRY_RUN=False, CANCEL_CONFIRM_THRESHOLD_PCT=5.0)
    actor = ExecutionActor(settings=settings)

    # Simulate 20 placements with 2 duplicate rejections → 10% rejection rate > 5%
    tracker = actor._tracker("tok1")
    for _ in range(20):
        tracker.record_placement()
    for _ in range(2):
        tracker.record_rejection()

    actor._update_confirm_cancel_mode("tok1")
    assert tracker.confirm_cancel_mode is True


@pytest.mark.asyncio
async def test_confirm_cancel_mode_deactivates_below_threshold():
    """Once in confirm-cancel mode, deactivates when rate drops below threshold."""
    settings = make_settings(DRY_RUN=False, CANCEL_CONFIRM_THRESHOLD_PCT=5.0)
    actor = ExecutionActor(settings=settings)

    tracker = actor._tracker("tok1")
    tracker.confirm_cancel_mode = True  # already in confirm-cancel mode

    # Simulate 100 placements, 0 rejections → 0% < 5%
    for _ in range(100):
        tracker.record_placement()

    actor._update_confirm_cancel_mode("tok1")
    assert tracker.confirm_cancel_mode is False


def test_rejection_rate_zero_when_no_placements():
    """No placements → rejection rate is 0.0."""
    settings = make_settings()
    actor = ExecutionActor(settings=settings)
    tracker = actor._tracker("tok1")
    assert tracker.rejection_rate_pct() == 0.0


def test_confirm_cancel_mode_default_is_false():
    """Confirm-cancel mode starts as fire-and-forget."""
    settings = make_settings()
    actor = ExecutionActor(settings=settings)
    assert actor._tracker("tok1").confirm_cancel_mode is False
