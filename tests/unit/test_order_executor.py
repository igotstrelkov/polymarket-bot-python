"""
Unit tests for the ExecutionActor in core/execution/execution_actor.py.

Covers DRY_RUN mode, live cancel/place, retry policy (10ms/25ms/50ms →
force reconciliation), and adaptive confirm-cancel mode activation/deactivation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from config.settings import Settings
from core.execution.execution_actor import (
    CancelMutation,
    ExecutionActor,
    PlaceMutation,
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


def make_intent(*, token_id: str = "tok1", side: str = "BUY") -> OrderIntent:
    return OrderIntent(
        token_id=token_id,
        side=side,
        price=0.49,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy="A",
        fee_rate_bps=78,
        neg_risk=False,
        tick_size=0.01,
    )


# ── DRY_RUN mode ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_place_does_not_call_api():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=True))
    clob  = AsyncMock()

    result = await actor.apply([PlaceMutation(intent=make_intent())], clob)

    clob.place_order.assert_not_awaited()
    assert len(result["placed"]) == 1


@pytest.mark.asyncio
async def test_dry_run_cancel_does_not_call_api():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=True))
    clob  = AsyncMock()

    result = await actor.apply([CancelMutation(order_id="oid1", token_id="tok1")], clob)

    clob.cancel_orders.assert_not_awaited()
    assert "oid1" in result["cancelled"]


@pytest.mark.asyncio
async def test_dry_run_empty_mutations_returns_empty_result():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=True))
    result = await actor.apply([], AsyncMock())
    assert result == {"cancelled": [], "placed": []}


# ── Live mode ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_place_calls_clob_and_returns_order_id():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.place_order = AsyncMock(return_value="order_xyz")

    result = await actor.apply([PlaceMutation(intent=make_intent())], clob)

    clob.place_order.assert_awaited_once()
    assert "order_xyz" in result["placed"]


@pytest.mark.asyncio
async def test_live_fire_and_forget_cancel_proceeds_to_place():
    """Cancel dispatched without awaiting; place proceeds immediately."""
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.cancel_orders = AsyncMock()
    clob.place_order   = AsyncMock(return_value="placed_id")

    mutations = [
        CancelMutation(order_id="c1", token_id="tok1"),
        PlaceMutation(intent=make_intent()),
    ]
    result = await actor.apply(mutations, clob)

    assert "c1"        in result["cancelled"]
    assert "placed_id" in result["placed"]


@pytest.mark.asyncio
async def test_live_multiple_places_all_returned():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.place_order = AsyncMock(side_effect=["id1", "id2"])

    mutations = [
        PlaceMutation(intent=make_intent(side="BUY")),
        PlaceMutation(intent=make_intent(side="SELL")),
    ]
    result = await actor.apply(mutations, clob)

    assert set(result["placed"]) == {"id1", "id2"}


# ── Retry policy ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    call_count = 0

    async def side_effect(_intent):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("duplicate order ID")
        return "retry_ok"

    clob.place_order = AsyncMock(side_effect=side_effect)

    with patch("core.execution.execution_actor.asyncio.sleep", new=AsyncMock()):
        result = await actor.apply([PlaceMutation(intent=make_intent())], clob)

    assert "retry_ok" in result["placed"]
    assert call_count == 2


@pytest.mark.asyncio
async def test_three_retries_exhausted_order_not_placed():
    """10ms → 25ms → 50ms all fail → order skipped, force reconciliation signalled."""
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.place_order = AsyncMock(side_effect=Exception("duplicate order ID"))

    with patch("core.execution.execution_actor.asyncio.sleep", new=AsyncMock()):
        result = await actor.apply([PlaceMutation(intent=make_intent())], clob)

    assert result["placed"] == []
    assert clob.place_order.await_count == 3


@pytest.mark.asyncio
async def test_retry_delays_are_10ms_25ms_50ms():
    """Verify the sleep calls use the correct backoff sequence."""
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.place_order = AsyncMock(side_effect=Exception("duplicate order ID"))

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("core.execution.execution_actor.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
        await actor.apply([PlaceMutation(intent=make_intent())], clob)

    assert sleep_calls == pytest.approx([0.010, 0.025, 0.050])


@pytest.mark.asyncio
async def test_non_duplicate_error_no_retry():
    """Errors other than duplicate-ID are not retried."""
    actor = ExecutionActor(settings=make_settings(DRY_RUN=False))
    clob  = AsyncMock()
    clob.place_order = AsyncMock(side_effect=Exception("500 internal server error"))

    result = await actor.apply([PlaceMutation(intent=make_intent())], clob)

    assert clob.place_order.await_count == 1
    assert result["placed"] == []


# ── Adaptive confirm-cancel mode ──────────────────────────────────────────────

def test_confirm_cancel_mode_default_off():
    actor = ExecutionActor(settings=make_settings())
    assert actor._tracker("tok1").confirm_cancel_mode is False


def test_rejection_rate_zero_before_any_placement():
    actor = ExecutionActor(settings=make_settings())
    assert actor._tracker("tok1").rejection_rate_pct() == 0.0


def test_confirm_cancel_activates_above_threshold():
    """10% rejection rate with threshold=5% → confirm-cancel activates."""
    actor   = ExecutionActor(settings=make_settings(CANCEL_CONFIRM_THRESHOLD_PCT=5.0))
    tracker = actor._tracker("tok1")
    for _ in range(10):
        tracker.record_placement()
    tracker.record_rejection()          # 1/10 = 10% > 5%
    actor._update_confirm_cancel_mode("tok1")
    assert tracker.confirm_cancel_mode is True


def test_confirm_cancel_deactivates_below_threshold():
    """Rate drops to 0% → exits confirm-cancel mode."""
    actor   = ExecutionActor(settings=make_settings(CANCEL_CONFIRM_THRESHOLD_PCT=5.0))
    tracker = actor._tracker("tok1")
    tracker.confirm_cancel_mode = True
    for _ in range(50):
        tracker.record_placement()      # 0 rejections → 0%
    actor._update_confirm_cancel_mode("tok1")
    assert tracker.confirm_cancel_mode is False


def test_confirm_cancel_stays_off_at_threshold():
    """Rate exactly at threshold (5%) does NOT activate (must be strictly >)."""
    actor   = ExecutionActor(settings=make_settings(CANCEL_CONFIRM_THRESHOLD_PCT=5.0))
    tracker = actor._tracker("tok1")
    for _ in range(20):
        tracker.record_placement()
    tracker.record_rejection()          # 1/20 = 5% == threshold (not >)
    actor._update_confirm_cancel_mode("tok1")
    assert tracker.confirm_cancel_mode is False


@pytest.mark.asyncio
async def test_confirm_cancel_mode_awaits_cancel_before_place():
    """In confirm-cancel mode, cancel_orders is awaited before place_order is called."""
    actor   = ExecutionActor(settings=make_settings(DRY_RUN=False))
    tracker = actor._tracker("tok1")
    tracker.confirm_cancel_mode = True

    call_log: list[str] = []
    async def log_cancel(*_args, **_kwargs):
        call_log.append("cancel")
    async def log_place(*_args, **_kwargs):
        call_log.append("place")
        return "placed_id"

    clob = AsyncMock()
    clob.cancel_orders = AsyncMock(side_effect=log_cancel)
    clob.place_order   = AsyncMock(side_effect=log_place)

    mutations = [
        CancelMutation(order_id="c1", token_id="tok1"),
        PlaceMutation(intent=make_intent()),
    ]
    await actor.apply(mutations, clob)

    assert call_log == ["cancel", "place"]
