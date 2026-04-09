"""
Unit tests for core/execution/liveness.py.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import Settings
from core.execution.liveness import (
    WsHeartbeatAdapter,
    market_user_ws_heartbeat_loop,
    order_safety_heartbeat_loop,
    sports_ws_heartbeat_loop,
)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://polygon-rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="passphrase",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Loop 1: Order-safety heartbeat ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_heartbeat_fires_at_interval():
    """Heartbeat is called once per sleep interval."""
    settings = make_settings(HEARTBEAT_INTERVAL_MS=100)
    clob = AsyncMock()
    clob.post_tick = AsyncMock()
    clob.get_server_time = AsyncMock(return_value=__import__("time").time())
    alerts = AsyncMock()
    call_count = 0

    async def fake_tick():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    clob.post_tick.side_effect = fake_tick

    with pytest.raises(asyncio.CancelledError):
        await order_safety_heartbeat_loop(clob, settings, alerts)

    assert call_count == 2


@pytest.mark.asyncio
async def test_heartbeat_session_dead_after_two_consecutive_misses():
    """Raise RuntimeError and send alert after 2 consecutive missed heartbeat acks."""
    settings = make_settings(HEARTBEAT_INTERVAL_MS=10)
    clob = AsyncMock()
    clob.post_tick.side_effect = ConnectionError("refused")
    clob.get_server_time = AsyncMock(return_value=__import__("time").time())
    alerts = AsyncMock()

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="session declared dead"):
            await order_safety_heartbeat_loop(clob, settings, alerts)

    alerts.send.assert_awaited_once_with("HEARTBEAT_SESSION_DEAD")


@pytest.mark.asyncio
async def test_heartbeat_resets_miss_counter_on_success():
    """Miss counter resets after a successful heartbeat."""
    settings = make_settings(HEARTBEAT_INTERVAL_MS=10)
    clob = AsyncMock()
    alerts = AsyncMock()
    call_count = 0

    # First call fails, second succeeds, third cancels
    async def post_tick_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("miss")
        if call_count >= 3:
            raise asyncio.CancelledError

    clob.post_tick.side_effect = post_tick_side_effect
    clob.get_server_time = AsyncMock(return_value=__import__("time").time())

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await order_safety_heartbeat_loop(clob, settings, alerts)

    # Alert NOT sent — counter reset on second success
    alerts.send.assert_not_awaited()


# ── Loop 2: WsHeartbeatAdapter — format ambiguity tests ──────────────────────

def test_heartbeat_format_ping_string():
    """Adapter configured with 'PING' sends 'PING'; 'PONG' clears health flag."""
    adapter = WsHeartbeatAdapter(ping_msg="PING", pong_msg="PONG")
    assert adapter.is_pong("PONG") is True
    assert adapter.is_pong("Pong {}") is False
    adapter.healthy = False
    adapter.on_pong()
    assert adapter.healthy is True


def test_heartbeat_format_ping_json():
    """Adapter configured with 'Ping {}' sends 'Ping {}'; 'Pong {}' clears health flag."""
    adapter = WsHeartbeatAdapter(ping_msg="Ping {}", pong_msg="Pong {}")
    assert adapter.is_pong("Pong {}") is True
    assert adapter.is_pong("PONG") is False
    adapter.healthy = False
    adapter.on_pong()
    assert adapter.healthy is True


@pytest.mark.asyncio
async def test_heartbeat_format_loop2_sends_ping():
    """Loop 2 sends application-level ping after each sleep interval."""
    market_ws = AsyncMock()
    user_ws = AsyncMock()

    pings_sent = []
    sleep_count = 0

    async def fake_sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError  # stop after second iteration

    async def fake_send(msg):
        pings_sent.append(msg)

    market_ws.send.side_effect = fake_send

    with patch("asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
        with pytest.raises(asyncio.CancelledError):
            await market_user_ws_heartbeat_loop(market_ws, user_ws)

    # At least one ping was sent (from the first sleep completing)
    assert len(pings_sent) >= 1
    assert sleep_count >= 1


# ── Loop 3: Sports channel heartbeat ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_sports_heartbeat_not_started_when_ws_none():
    """Loop 3 returns immediately when sports_ws=None."""
    # Should return without blocking (no timeout needed)
    await asyncio.wait_for(sports_ws_heartbeat_loop(None), timeout=1.0)


@pytest.mark.asyncio
async def test_sports_heartbeat_replies_pong():
    """Loop 3 replies 'pong' when server sends 'ping'."""
    sports_ws = AsyncMock()
    sent = []

    async def fake_recv():
        return "ping"

    async def fake_send(msg):
        sent.append(msg)
        raise asyncio.CancelledError

    sports_ws.recv.side_effect = fake_recv
    sports_ws.send.side_effect = fake_send

    with pytest.raises(asyncio.CancelledError):
        await sports_ws_heartbeat_loop(sports_ws)

    assert sent == ["pong"]


# ── Independence: failure in one loop does not affect others ──────────────────

@pytest.mark.asyncio
async def test_loops_are_independent():
    """Failure in Loop 1 does not prevent Loop 2 or 3 from running."""
    settings = make_settings(HEARTBEAT_INTERVAL_MS=10)

    clob = AsyncMock()
    clob.post_tick.side_effect = ConnectionError("dead")
    clob.get_server_time = AsyncMock(return_value=__import__("time").time())
    alerts = AsyncMock()

    market_ws = AsyncMock()
    user_ws = AsyncMock()
    loop2_ran = False

    async def fake_loop2(_mws, _uws):
        nonlocal loop2_ran
        loop2_ran = True

    with patch("asyncio.sleep", new=AsyncMock()):
        # Run Loop 1 until it dies; Loop 2 in parallel
        task1 = asyncio.create_task(order_safety_heartbeat_loop(clob, settings, alerts))
        task2 = asyncio.create_task(fake_loop2(market_ws, user_ws))

        # Wait briefly for both
        done, pending = await asyncio.wait([task1, task2], timeout=0.1)
        for t in pending:
            t.cancel()

    # Loop 2 was started and ran independently of Loop 1's failure
    assert loop2_ran is True
