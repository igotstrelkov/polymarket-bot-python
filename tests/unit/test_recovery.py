"""
Unit tests for core/ledger/recovery_coordinator.py.

Covers: successful recovery (reconciliation of ledger vs. CLOB open orders),
HTTP failure handling, is_resyncing flag, stub records for crash-orphan orders,
and cancellation of orders no longer on the exchange.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.ledger.order_ledger import OrderLedger, OrderState
from core.ledger.recovery_coordinator import RecoveryCoordinator


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_coordinator() -> tuple[OrderLedger, RecoveryCoordinator]:
    ledger = OrderLedger()
    coordinator = RecoveryCoordinator(ledger)
    return ledger, coordinator


def submit_order(ledger: OrderLedger, order_id: str, **kwargs) -> None:
    ledger.record_submitted(
        order_id=order_id,
        token_id=kwargs.get("token_id", "tok1"),
        side=kwargs.get("side", "BUY"),
        price=kwargs.get("price", 0.50),
        size=kwargs.get("size", 10.0),
        time_in_force="GTC",
        post_only=True,
        strategy="A",
        fee_rate_bps=78,
        neg_risk=False,
    )


def make_clob_client(open_order_ids: list[str]) -> AsyncMock:
    clob = AsyncMock()
    clob.get_orders = AsyncMock(return_value=[{"id": oid} for oid in open_order_ids])
    return clob


# ── Successful recovery ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_success_flag():
    ledger, coordinator = make_coordinator()
    clob = make_clob_client([])
    result = await coordinator.recover(clob)
    assert result.success is True


@pytest.mark.asyncio
async def test_recovery_order_still_live_marked_acknowledged():
    ledger, coordinator = make_coordinator()
    submit_order(ledger, "o1")
    clob = make_clob_client(["o1"])

    result = await coordinator.recover(clob)

    assert result.success is True
    assert "o1" in result.recovered_order_ids
    assert ledger.get("o1").state == OrderState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_recovery_order_not_on_exchange_cancelled():
    """Order in ledger but not in open-orders response → cancelled."""
    ledger, coordinator = make_coordinator()
    submit_order(ledger, "o1")
    clob = make_clob_client([])   # empty response — o1 no longer live

    await coordinator.recover(clob)

    rec = ledger.get("o1")
    assert rec.state == OrderState.CANCELLED
    assert "recovery" in rec.cancel_reason


@pytest.mark.asyncio
async def test_recovery_unknown_order_creates_stub():
    """Order on exchange but not in ledger → stub record created."""
    ledger, coordinator = make_coordinator()
    clob = make_clob_client(["orphan_id"])

    result = await coordinator.recover(clob)

    assert "orphan_id" in result.recovered_order_ids
    rec = ledger.get("orphan_id")
    assert rec is not None
    assert rec.state == OrderState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_recovery_multiple_orders_reconciled_correctly():
    ledger, coordinator = make_coordinator()
    submit_order(ledger, "alive")
    submit_order(ledger, "dead")
    clob = make_clob_client(["alive"])

    result = await coordinator.recover(clob)

    assert ledger.get("alive").state == OrderState.ACKNOWLEDGED
    assert ledger.get("dead").state == OrderState.CANCELLED
    assert "alive" in result.recovered_order_ids
    assert "dead" not in result.recovered_order_ids


# ── HTTP failure handling ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recovery_failure_when_clob_raises():
    ledger, coordinator = make_coordinator()
    clob = AsyncMock()
    clob.get_orders = AsyncMock(side_effect=Exception("connection refused"))

    result = await coordinator.recover(clob)

    assert result.success is False
    assert result.error != ""
    assert result.recovered_order_ids == []


@pytest.mark.asyncio
async def test_resyncing_false_after_failure():
    """is_resyncing() must return False even after a failed recovery."""
    ledger, coordinator = make_coordinator()
    clob = AsyncMock()
    clob.get_orders = AsyncMock(side_effect=Exception("timeout"))

    await coordinator.recover(clob)

    assert coordinator.is_resyncing() is False


# ── is_resyncing() ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resyncing_false_after_successful_recovery():
    ledger, coordinator = make_coordinator()
    await coordinator.recover(make_clob_client([]))
    assert coordinator.is_resyncing() is False


def test_resyncing_false_initially():
    _, coordinator = make_coordinator()
    assert coordinator.is_resyncing() is False


# ── confirmed_order_ids / last_recovery ──────────────────────────────────────

def test_confirmed_order_ids_empty_before_recovery():
    _, coordinator = make_coordinator()
    assert coordinator.confirmed_order_ids() == []


@pytest.mark.asyncio
async def test_last_recovery_none_before_recovery():
    _, coordinator = make_coordinator()
    assert coordinator.last_recovery() is None


@pytest.mark.asyncio
async def test_last_recovery_populated_after_recovery():
    ledger, coordinator = make_coordinator()
    await coordinator.recover(make_clob_client([]))
    assert coordinator.last_recovery() is not None
    assert coordinator.last_recovery().success is True
