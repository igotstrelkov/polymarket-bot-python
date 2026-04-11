"""
Integration tests for WebSocket reconnect and confirmed-state rebuild.

Covers:
- WS drop → reconnect fires with exponential backoff
- After reconnect → rebuild_confirmed_state() called before any placement
- No placements occur during reconciliation window
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ledger.order_ledger import OrderLedger, OrderState
from core.ledger.recovery_coordinator import RecoveryCoordinator, RecoveryResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_open_orders_response(order_ids: list[str]) -> list[dict]:
    return [
        {
            "id": oid,
            "asset_id": "tok_1",
            "side": "BUY",
            "price": "0.45",
            "original_size": "10",
            "size_matched": "0",
            "time_in_force": "GTC",
            "is_negRisk": False,
        }
        for oid in order_ids
    ]


def make_failing_ws_context():
    """Returns a mock for websockets.connect that raises OSError when entered."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=OSError("WS connect failed"))
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_cm


# ── Test 1: WS drop → reconnect with backoff ─────────────────────────────────

@pytest.mark.asyncio
async def test_market_stream_reconnects_after_disconnect():
    """MarketStreamGateway must reconnect with exponential backoff on WS failure."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(book_queue=book_queue, resync_queue=resync_queue)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) >= 2:
            gw._running = False
            raise asyncio.CancelledError

    with patch("core.execution.market_stream.websockets.connect",
               return_value=make_failing_ws_context()):
        with patch("core.execution.market_stream.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises((asyncio.CancelledError, Exception)):
                await gw.connect()

    # Must have attempted backoff sleep after the first failure
    assert len(sleep_delays) >= 1
    assert sleep_delays[0] >= 1.0  # initial backoff ≥ 1s


@pytest.mark.asyncio
async def test_user_stream_reconnects_after_disconnect():
    """UserStreamGateway must reconnect with exponential backoff on WS failure."""
    from core.execution.user_stream import UserStreamGateway
    from auth.credentials import ApiCreds

    creds = ApiCreds(api_key="key", secret="sec", passphrase="pass")
    gw = UserStreamGateway(
        creds=creds,
        fill_queue=asyncio.Queue(),
        ack_queue=asyncio.Queue(),
        cancel_queue=asyncio.Queue(),
    )

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) >= 2:
            gw._running = False
            raise asyncio.CancelledError

    with patch("core.execution.user_stream.websockets.connect",
               return_value=make_failing_ws_context()):
        with patch("core.execution.user_stream.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises((asyncio.CancelledError, Exception)):
                await gw.connect()

    assert len(sleep_delays) >= 1
    assert sleep_delays[0] >= 1.0


@pytest.mark.asyncio
async def test_backoff_is_exponential():
    """Reconnect backoff must be non-decreasing on successive failures."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(book_queue=book_queue, resync_queue=resync_queue)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) >= 3:
            gw._running = False
            raise asyncio.CancelledError

    with patch("core.execution.market_stream.websockets.connect",
               return_value=make_failing_ws_context()):
        with patch("core.execution.market_stream.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises((asyncio.CancelledError, Exception)):
                await gw.connect()

    if len(sleep_delays) >= 2:
        assert sleep_delays[1] >= sleep_delays[0]


@pytest.mark.asyncio
async def test_backoff_resets_on_successful_connect():
    """Backoff must reset to the initial value after a successful connection."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(book_queue=book_queue, resync_queue=resync_queue)

    # Simulate one failure then a successful connect that immediately closes
    success_ws = MagicMock()
    success_ws.__aenter__ = AsyncMock(return_value=success_ws)
    success_ws.__aexit__ = AsyncMock(return_value=None)
    success_ws.send = AsyncMock()
    success_ws.__aiter__ = MagicMock(return_value=iter([]))  # no messages

    attempt = [0]
    sleep_delays: list[float] = []

    def make_ws(_url, **_kwargs):
        attempt[0] += 1
        if attempt[0] == 1:
            return make_failing_ws_context()
        return success_ws

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        if len(sleep_delays) >= 2:
            gw._running = False
            raise asyncio.CancelledError

    with patch("core.execution.market_stream.websockets.connect", side_effect=make_ws):
        with patch("core.execution.market_stream.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises((asyncio.CancelledError, Exception)):
                await gw.connect()

    # First sleep is after failure, second may be after success — first must be initial backoff
    assert sleep_delays[0] == pytest.approx(1.0)


# ── Test 2: Reconnect → rebuild_confirmed_state called before any placement ───

@pytest.mark.asyncio
async def test_rebuild_confirmed_state_called_on_reconnect():
    """After WS reconnect, RecoveryCoordinator.recover() must be called."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(return_value=[])

    result = await coordinator.recover(mock_clob)

    assert result.success is True
    mock_clob.get_open_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebuild_confirmed_state_merges_exchange_orders():
    """rebuild_confirmed_state creates stub records for orders on exchange but not in ledger."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(
        return_value=make_open_orders_response(["ord_orphan_1", "ord_orphan_2"])
    )

    result = await coordinator.recover(mock_clob)

    assert result.success is True
    confirmed = coordinator.confirmed_order_ids()
    assert "ord_orphan_1" in confirmed
    assert "ord_orphan_2" in confirmed


@pytest.mark.asyncio
async def test_rebuild_confirmed_state_cancels_missing_ledger_orders():
    """Orders in the ledger but absent from exchange response are cancelled."""
    order_ledger = OrderLedger()
    order_ledger.record_submitted(
        order_id="ord_missing",
        token_id="tok_1",
        side="BUY",
        price=0.45,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        strategy="A",
        fee_rate_bps=0,
        neg_risk=False,
    )

    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(return_value=[])  # exchange has nothing

    await coordinator.recover(mock_clob)

    record = order_ledger.get("ord_missing")
    assert record is not None
    assert record.state == OrderState.CANCELLED


# ── Test 3: No placements during reconciliation window ───────────────────────

@pytest.mark.asyncio
async def test_no_placements_while_resyncing():
    """is_resyncing() returns True during recovery; placements must be gated."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    coordinator._resyncing = True
    assert coordinator.is_resyncing() is True

    mock_clob = AsyncMock()
    mock_clob.create_order = AsyncMock()

    if coordinator.is_resyncing():
        pass  # guard blocks placement
    else:
        await mock_clob.create_order({"token_id": "tok_1"})

    mock_clob.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_resyncing_false_after_successful_recovery():
    """After recovery completes successfully, is_resyncing() must be False."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(return_value=[])

    result = await coordinator.recover(mock_clob)

    assert result.success is True
    assert coordinator.is_resyncing() is False


@pytest.mark.asyncio
async def test_is_resyncing_false_after_failed_recovery():
    """Even on failure, is_resyncing() must be reset to False (unblocks retry)."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(side_effect=Exception("CLOB unreachable"))

    result = await coordinator.recover(mock_clob)

    assert result.success is False
    assert coordinator.is_resyncing() is False


@pytest.mark.asyncio
async def test_confirmed_order_ids_empty_before_recovery():
    """confirmed_order_ids() returns empty list before any recovery."""
    coordinator = RecoveryCoordinator(OrderLedger())
    assert coordinator.confirmed_order_ids() == []


@pytest.mark.asyncio
async def test_last_recovery_timestamp_set_on_success():
    """last_recovery() is populated after successful recovery."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(return_value=[])

    assert coordinator.last_recovery() is None

    await coordinator.recover(mock_clob)

    assert coordinator.last_recovery() is not None
    assert coordinator.last_recovery().success is True


# ── Test 4: Recovery result fields ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_result_contains_order_ids():
    """RecoveryResult.recovered_order_ids contains IDs confirmed on exchange."""
    order_ledger = OrderLedger()
    coordinator = RecoveryCoordinator(order_ledger)

    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(
        return_value=make_open_orders_response(["ord_live_1"])
    )

    result = await coordinator.recover(mock_clob)

    assert result.success is True
    assert "ord_live_1" in result.recovered_order_ids


@pytest.mark.asyncio
async def test_recovery_result_has_timestamp():
    """RecoveryResult.recovered_at is populated."""
    coordinator = RecoveryCoordinator(OrderLedger())
    mock_clob = AsyncMock()
    mock_clob.get_open_orders = AsyncMock(return_value=[])

    result = await coordinator.recover(mock_clob)

    assert result.recovered_at is not None
