"""
Integration tests for Builder Relayer EOA failover (FR-216).

Covers:
- Relayer unreachable for EOA_FALLBACK_TIMEOUT_S → EOA activated, alert sent
- Recovery → reverts to Relayer execution, RELAYER_RECOVERED alert sent
- Partial outage window (< EOA_FALLBACK_TIMEOUT_S) → exception propagated
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from auth.relayer import FailoverState, submit_with_failover
from config.settings import Settings


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings(
        DRY_RUN=True,
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://polygon-rpc.example.com",
        BUILDER_API_KEY="test-api-key",
        BUILDER_SECRET="test-secret",
        BUILDER_PASSPHRASE="test-passphrase",
        EOA_FALLBACK_TIMEOUT_S=30,
    )


def make_relayer(fail: bool = False) -> AsyncMock:
    relayer = AsyncMock()
    if fail:
        relayer.execute = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    else:
        relayer.execute = AsyncMock(return_value={"status": "ok"})
    return relayer


def make_eoa_client() -> AsyncMock:
    eoa = AsyncMock()
    eoa.create_and_post_order = AsyncMock(return_value={"status": "eoa_ok"})
    return eoa


def make_alerts() -> AsyncMock:
    alerts = AsyncMock()
    alerts.send = AsyncMock()
    return alerts


# ── Test 1: Relayer down for EOA_FALLBACK_TIMEOUT_S → EOA activated ──────────

@pytest.mark.asyncio
async def test_eoa_activated_after_timeout(settings):
    """After relayer has been down for EOA_FALLBACK_TIMEOUT_S, EOA mode activates."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()

    # Pre-set relayer_down_since to simulate timeout already elapsed
    state.relayer_down_since = time.monotonic() - settings.EOA_FALLBACK_TIMEOUT_S - 1.0

    result = await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert state.eoa_active is True
    assert result == {"status": "eoa_ok"}


@pytest.mark.asyncio
async def test_failover_alert_sent_on_eoa_activation(settings):
    """RELAYER_FAILOVER_ACTIVATED alert is sent when EOA activates."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()
    state.relayer_down_since = time.monotonic() - settings.EOA_FALLBACK_TIMEOUT_S - 1.0

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    alerts.send.assert_awaited_once()
    call_arg = alerts.send.call_args[0][0]
    assert "FAILOVER" in call_arg or "RELAYER" in call_arg


@pytest.mark.asyncio
async def test_eoa_client_called_when_active(settings):
    """When EOA mode is active, eoa_client.create_and_post_order is called."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()
    state.relayer_down_since = time.monotonic() - settings.EOA_FALLBACK_TIMEOUT_S - 1.0

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    eoa.create_and_post_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_outage_raises_not_eoa(settings):
    """If relayer has been down < EOA_FALLBACK_TIMEOUT_S, exception is propagated."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()
    # Outage started 5s ago — well below 30s timeout
    state.relayer_down_since = time.monotonic() - 5.0

    with pytest.raises(httpx.ConnectError):
        await submit_with_failover(
            {"token_id": "tok_1"},
            relayer_client=relayer,
            eoa_client=eoa,
            settings=settings,
            alerts=alerts,
            _state=state,
        )

    assert state.eoa_active is False
    eoa.create_and_post_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_failure_sets_down_since_timestamp(settings):
    """First relayer failure initialises relayer_down_since."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()

    assert state.relayer_down_since is None

    before = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        await submit_with_failover(
            {"token_id": "tok_1"},
            relayer_client=relayer,
            eoa_client=eoa,
            settings=settings,
            alerts=alerts,
            _state=state,
        )
    after = time.monotonic()

    assert state.relayer_down_since is not None
    assert before <= state.relayer_down_since <= after


# ── Test 2: Recovery → reverts to Relayer, RELAYER_RECOVERED alert ────────────

@pytest.mark.asyncio
async def test_eoa_recovery_reverts_to_relayer(settings):
    """When relayer recovers while EOA is active, eoa_active is cleared."""
    relayer = make_relayer(fail=False)  # relayer now succeeds
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60.0)

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert state.eoa_active is False
    assert state.relayer_down_since is None


@pytest.mark.asyncio
async def test_recovered_alert_sent_on_relayer_recovery(settings):
    """RELAYER_RECOVERED alert is sent when relayer comes back."""
    relayer = make_relayer(fail=False)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60.0)

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    alerts.send.assert_awaited_once()
    call_arg = alerts.send.call_args[0][0]
    assert "RECOVERED" in call_arg or "RELAYER" in call_arg


@pytest.mark.asyncio
async def test_relayer_used_for_order_on_recovery(settings):
    """On recovery, the order is submitted via relayer, not EOA."""
    relayer = make_relayer(fail=False)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60.0)

    result = await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "ok"}
    eoa.create_and_post_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_eoa_stays_active_if_relayer_still_failing(settings):
    """If relayer still fails during recovery probe, EOA stays active."""
    relayer = make_relayer(fail=True)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState(eoa_active=True, relayer_down_since=time.monotonic() - 60.0)

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert state.eoa_active is True
    eoa.create_and_post_order.assert_awaited_once()


# ── Test 3: Normal path success ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_normal_relayer_success_returns_result(settings):
    """Normal relayer success returns the relayer result."""
    relayer = make_relayer(fail=False)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()

    result = await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert result == {"status": "ok"}
    eoa.create_and_post_order.assert_not_awaited()
    alerts.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_success_clears_down_since(settings):
    """Successful relayer call clears the relayer_down_since partial-outage timer."""
    relayer = make_relayer(fail=False)
    eoa = make_eoa_client()
    alerts = make_alerts()
    state = FailoverState()
    state.relayer_down_since = time.monotonic() - 5.0  # partial outage started

    await submit_with_failover(
        {"token_id": "tok_1"},
        relayer_client=relayer,
        eoa_client=eoa,
        settings=settings,
        alerts=alerts,
        _state=state,
    )

    assert state.relayer_down_since is None
