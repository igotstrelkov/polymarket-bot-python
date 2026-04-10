"""
Unit tests for core/ledger/order_ledger.py.

Covers: full lifecycle transitions, open_orders filtering, history tracking,
unknown order transitions, and terminal state set membership.
"""

from __future__ import annotations

import pytest

from core.ledger.order_ledger import OrderLedger, OrderState


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_ledger() -> OrderLedger:
    return OrderLedger()


def submit(ledger: OrderLedger, order_id: str = "oid1") -> None:
    ledger.record_submitted(
        order_id=order_id,
        token_id="tok1",
        side="BUY",
        price=0.50,
        size=10.0,
        time_in_force="GTC",
        post_only=True,
        strategy="A",
        fee_rate_bps=78,
        neg_risk=False,
    )


# ── record_submitted ──────────────────────────────────────────────────────────

def test_submit_creates_record():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.get("oid1")
    assert rec is not None
    assert rec.state == OrderState.SUBMITTED
    assert rec.order_id == "oid1"


def test_submit_populates_fields():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.get("oid1")
    assert rec.side == "BUY"
    assert rec.price == pytest.approx(0.50)
    assert rec.size == pytest.approx(10.0)
    assert rec.strategy == "A"
    assert rec.fee_rate_bps == 78


# ── state transitions ─────────────────────────────────────────────────────────

def test_acknowledged_transition():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_acknowledged("oid1")
    assert rec is not None
    assert rec.state == OrderState.ACKNOWLEDGED


def test_filled_transition():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_filled("oid1", filled_size=10.0)
    assert rec is not None
    assert rec.state == OrderState.FILLED
    assert rec.filled_size == pytest.approx(10.0)


def test_partially_filled_transition():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_partially_filled("oid1", filled_size=5.0)
    assert rec.state == OrderState.PARTIALLY_FILLED
    assert rec.filled_size == pytest.approx(5.0)


def test_cancelled_transition_with_reason():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_cancelled("oid1", reason="user_request")
    assert rec.state == OrderState.CANCELLED
    assert rec.cancel_reason == "user_request"


def test_expired_transition():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_expired("oid1")
    assert rec.state == OrderState.EXPIRED


def test_rejected_transition_with_reason():
    ledger = make_ledger()
    submit(ledger)
    rec = ledger.record_rejected("oid1", reason="duplicate order ID")
    assert rec.state == OrderState.REJECTED
    assert "duplicate" in rec.cancel_reason


# ── unknown order transitions ─────────────────────────────────────────────────

def test_transition_on_unknown_order_returns_none():
    ledger = make_ledger()
    result = ledger.record_acknowledged("nonexistent")
    assert result is None


# ── open_orders ───────────────────────────────────────────────────────────────

def test_open_orders_excludes_terminal():
    ledger = make_ledger()
    submit(ledger, "o1")
    submit(ledger, "o2")
    submit(ledger, "o3")
    ledger.record_filled("o1", filled_size=10.0)
    ledger.record_cancelled("o2")
    ledger.record_acknowledged("o3")
    open_ids = {r.order_id for r in ledger.open_orders()}
    assert "o3" in open_ids
    assert "o1" not in open_ids
    assert "o2" not in open_ids


def test_open_orders_empty_initially():
    ledger = make_ledger()
    assert ledger.open_orders() == []


def test_open_orders_includes_submitted_and_acknowledged():
    ledger = make_ledger()
    submit(ledger, "o1")
    ledger.record_acknowledged("o1")
    submit(ledger, "o2")
    open_ids = {r.order_id for r in ledger.open_orders()}
    assert open_ids == {"o1", "o2"}


# ── history ───────────────────────────────────────────────────────────────────

def test_history_records_all_transitions():
    ledger = make_ledger()
    submit(ledger, "o1")
    ledger.record_acknowledged("o1")
    ledger.record_filled("o1", filled_size=10.0)
    hist = ledger.history("o1")
    states = [r.state for r in hist]
    assert OrderState.SUBMITTED in states
    assert OrderState.ACKNOWLEDGED in states
    assert OrderState.FILLED in states


def test_history_empty_for_unknown_order():
    ledger = make_ledger()
    assert ledger.history("unknown") == []


def test_all_records_returns_all():
    ledger = make_ledger()
    submit(ledger, "o1")
    submit(ledger, "o2")
    ids = {r.order_id for r in ledger.all_records()}
    assert ids == {"o1", "o2"}
