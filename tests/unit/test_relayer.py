"""
Unit tests for auth/relayer.py.

All HTTP calls (RelayClient) and Redis calls are mocked with AsyncMock.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from auth.relayer import FailoverState, RelayClient, get_or_deploy_safe, submit_with_failover
from config.settings import Settings


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


# ── get_or_deploy_safe ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redis_hit_returns_address_without_deploy():
    settings = make_settings()
    redis = AsyncMock()
    redis.get.return_value = "0xSafeAddress"

    relay = AsyncMock(spec=RelayClient)

    address = await get_or_deploy_safe(settings, redis, relay_client=relay)

    assert address == "0xSafeAddress"
    relay.get_deployed.assert_not_awaited()
    relay.deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_miss_get_deployed_hit_returns_address_without_deploy():
    """Redis miss but Safe already deployed — no deploy() call."""
    settings = make_settings()
    redis = AsyncMock()
    redis.get.return_value = None  # cache miss

    relay = AsyncMock(spec=RelayClient)
    relay.get_deployed.return_value = "0xAlreadyDeployed"

    address = await get_or_deploy_safe(settings, redis, relay_client=relay)

    assert address == "0xAlreadyDeployed"
    relay.deploy.assert_not_awaited()
    redis.set.assert_awaited_once_with("wallet:safe_address", "0xAlreadyDeployed")


@pytest.mark.asyncio
async def test_redis_miss_get_deployed_miss_calls_deploy_and_caches():
    """Redis miss + not deployed → deploy() called and address cached."""
    settings = make_settings()
    redis = AsyncMock()
    redis.get.return_value = None

    relay = AsyncMock(spec=RelayClient)
    relay.get_deployed.return_value = None           # not yet deployed
    relay.deploy.return_value = "0xNewSafe"

    address = await get_or_deploy_safe(settings, redis, relay_client=relay)

    assert address == "0xNewSafe"
    relay.deploy.assert_awaited_once()
    redis.set.assert_awaited_once_with("wallet:safe_address", "0xNewSafe")


@pytest.mark.asyncio
async def test_redis_hit_bytes_decoded():
    """Redis may return bytes; decoded to str transparently."""
    settings = make_settings()
    redis = AsyncMock()
    redis.get.return_value = b"0xBytesAddress"

    relay = AsyncMock(spec=RelayClient)

    address = await get_or_deploy_safe(settings, redis, relay_client=relay)

    assert address == "0xBytesAddress"


# ── submit_with_failover ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_succeeds_via_relayer():
    settings = make_settings()
    relay = AsyncMock(spec=RelayClient)
    relay.execute.return_value = {"status": "ok"}
    eoa = AsyncMock()
    alerts = AsyncMock()
    state = FailoverState()

    result = await submit_with_failover(
        order={"id": "1"},
        relayer_client=relay,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "ok"}
    eoa.create_and_post_order.assert_not_awaited()
    alerts.send.assert_not_awaited()
    assert state.eoa_active is False


@pytest.mark.asyncio
async def test_relayer_unreachable_below_timeout_raises():
    """Relayer failure within EOA_FALLBACK_TIMEOUT_S propagates the error."""
    settings = make_settings(EOA_FALLBACK_TIMEOUT_S=30)
    relay = AsyncMock(spec=RelayClient)
    relay.execute.side_effect = httpx.ConnectError("refused")
    eoa = AsyncMock()
    alerts = AsyncMock()
    # Outage started 5 seconds ago — well within the 30-second timeout
    state = FailoverState(relayer_down_since=time.monotonic() - 5)

    with pytest.raises(httpx.ConnectError):
        await submit_with_failover(
            order={"id": "2"},
            relayer_client=relay,
            eoa_client=eoa,
            settings=settings,
            alerts=alerts,
            _state=state,
        )

    assert state.eoa_active is False
    eoa.create_and_post_order.assert_not_awaited()
    alerts.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_relayer_unreachable_past_timeout_activates_eoa_and_alerts():
    """FR-216: after EOA_FALLBACK_TIMEOUT_S of outage, switch to EOA and alert."""
    settings = make_settings(EOA_FALLBACK_TIMEOUT_S=30)
    relay = AsyncMock(spec=RelayClient)
    relay.execute.side_effect = httpx.ConnectError("refused")
    eoa = AsyncMock()
    eoa.create_and_post_order.return_value = {"status": "eoa_ok"}
    alerts = AsyncMock()

    # Simulate outage that started just past the timeout threshold
    state = FailoverState(
        relayer_down_since=time.monotonic() - settings.EOA_FALLBACK_TIMEOUT_S - 1
    )

    result = await submit_with_failover(
        order={"id": "3"},
        relayer_client=relay,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "eoa_ok"}
    assert state.eoa_active is True
    eoa.create_and_post_order.assert_awaited_once()
    alerts.send.assert_awaited_once()
    sent_event = alerts.send.call_args[0][0]
    assert "FAILOVER" in sent_event or "RELAYER" in sent_event


@pytest.mark.asyncio
async def test_eoa_active_relayer_still_down_stays_on_eoa():
    """While EOA is active and relayer is still unreachable, stay on EOA."""
    settings = make_settings()
    relay = AsyncMock(spec=RelayClient)
    relay.execute.side_effect = httpx.ConnectError("still down")
    eoa = AsyncMock()
    eoa.create_and_post_order.return_value = {"status": "eoa_ok"}
    alerts = AsyncMock()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60)

    result = await submit_with_failover(
        order={"id": "4"},
        relayer_client=relay,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "eoa_ok"}
    assert state.eoa_active is True  # remains on EOA
    alerts.send.assert_not_awaited()  # no new alert while already on EOA


@pytest.mark.asyncio
async def test_relayer_recovery_reverts_to_relayer_and_alerts():
    """When EOA is active and relayer recovers, revert and send RECOVERED alert."""
    settings = make_settings()
    relay = AsyncMock(spec=RelayClient)
    relay.execute.return_value = {"status": "relayer_ok"}  # relayer back up
    eoa = AsyncMock()
    alerts = AsyncMock()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60)

    result = await submit_with_failover(
        order={"id": "5"},
        relayer_client=relay,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "relayer_ok"}
    assert state.eoa_active is False
    assert state.relayer_down_since is None
    eoa.create_and_post_order.assert_not_awaited()
    alerts.send.assert_awaited_once()
    sent_event = alerts.send.call_args[0][0]
    assert "RECOVER" in sent_event or "RELAYER" in sent_event
