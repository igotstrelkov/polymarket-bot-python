"""
Unit tests for core/ledger/fill_position_ledger.py.

Covers: fill recording, position updates (BUY/SELL), realized P&L,
30-second markout (FR-601a), pending markout tracking, and fill counts.
"""

from __future__ import annotations

import pytest

from core.ledger.fill_position_ledger import FillAndPositionLedger


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_ledger() -> FillAndPositionLedger:
    return FillAndPositionLedger()


def record_fill(
    ledger: FillAndPositionLedger,
    fill_id: str = "f1",
    *,
    side: str = "BUY",
    price: float = 0.50,
    size: float = 10.0,
    strategy: str = "A",
    is_maker: bool = True,
    mid_at_fill: float | None = 0.50,
    token_id: str = "tok1",
):
    return ledger.record_fill(
        fill_id=fill_id,
        order_id="oid1",
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        fee_paid=0.0,
        strategy=strategy,
        is_maker=is_maker,
        mid_at_fill=mid_at_fill,
    )


# ── Fill recording ────────────────────────────────────────────────────────────

def test_record_fill_stores_record():
    ledger = make_ledger()
    fill = record_fill(ledger)
    assert ledger.get_fill("f1") is not None
    assert fill.fill_id == "f1"
    assert fill.side == "BUY"


def test_fill_count_increments():
    ledger = make_ledger()
    record_fill(ledger, "f1")
    record_fill(ledger, "f2")
    assert ledger.fill_count() == 2


def test_fill_count_by_strategy():
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="A")
    record_fill(ledger, "f2", strategy="B")
    assert ledger.fill_count("A") == 1
    assert ledger.fill_count("B") == 1
    assert ledger.fill_count("C") == 0


def test_fills_for_order():
    ledger = make_ledger()
    record_fill(ledger, "f1")
    result = ledger.fills_for_order("oid1")
    assert len(result) == 1
    assert result[0].fill_id == "f1"


# ── Position updates ──────────────────────────────────────────────────────────

def test_buy_increases_position():
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY", size=10.0, price=0.50)
    pos = ledger.get_position("tok1")
    assert pos is not None
    assert pos.shares == pytest.approx(10.0)


def test_sell_decreases_position():
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY", size=10.0, price=0.50)
    record_fill(ledger, "f2", side="SELL", size=5.0, price=0.60)
    pos = ledger.get_position("tok1")
    assert pos.shares == pytest.approx(5.0)


def test_avg_entry_price_updated_on_buy():
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY", size=10.0, price=0.40)
    record_fill(ledger, "f2", side="BUY", size=10.0, price=0.60)
    pos = ledger.get_position("tok1")
    # avg = (10*0.40 + 10*0.60) / 20 = 0.50
    assert pos.avg_entry_price == pytest.approx(0.50)


def test_realized_pnl_on_sell():
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY",  size=10.0, price=0.40)
    record_fill(ledger, "f2", side="SELL", size=10.0, price=0.60)
    pos = ledger.get_position("tok1")
    # pnl = (0.60 - 0.40) * 10 = 2.0
    assert pos.realized_pnl == pytest.approx(2.0)


def test_total_realized_pnl_across_tokens():
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY",  size=10.0, price=0.40, token_id="a")
    record_fill(ledger, "f2", side="SELL", size=10.0, price=0.60, token_id="a")
    record_fill(ledger, "f3", side="BUY",  size=5.0,  price=0.50, token_id="b")
    record_fill(ledger, "f4", side="SELL", size=5.0,  price=0.70, token_id="b")
    # a: 2.0, b: 1.0
    assert ledger.total_realized_pnl() == pytest.approx(3.0)


def test_position_none_before_any_fill():
    ledger = make_ledger()
    assert ledger.get_position("tok_new") is None


def test_sell_floors_shares_at_zero():
    """Selling more than held floors shares at zero (no negative positions)."""
    ledger = make_ledger()
    record_fill(ledger, "f1", side="BUY", size=5.0, price=0.50)
    record_fill(ledger, "f2", side="SELL", size=10.0, price=0.60)
    pos = ledger.get_position("tok1")
    assert pos.shares == pytest.approx(0.0)


# ── 30-second markout (FR-601a) ───────────────────────────────────────────────

def test_strategy_a_fill_queued_for_markout():
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="A", mid_at_fill=0.50)
    assert "f1" in ledger.pending_markout_fill_ids()


def test_non_strategy_a_fill_not_queued():
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="B", mid_at_fill=0.50)
    assert "f1" not in ledger.pending_markout_fill_ids()


def test_record_markout_buy_adverse():
    """BUY fill: mid goes up → adverse (positive markout)."""
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="A", side="BUY", mid_at_fill=0.50)
    ledger.record_markout("f1", mid_at_t30=0.52)
    fill = ledger.get_fill("f1")
    # markout = (0.52 - 0.50) * +1 = +0.02 (adverse)
    assert fill.markout_30s == pytest.approx(0.02)


def test_record_markout_sell_adverse():
    """SELL fill: mid goes down → adverse (positive markout)."""
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="A", side="SELL", mid_at_fill=0.50)
    ledger.record_markout("f1", mid_at_t30=0.48)
    fill = ledger.get_fill("f1")
    # markout = (0.48 - 0.50) * -1 = +0.02 (adverse)
    assert fill.markout_30s == pytest.approx(0.02)


def test_record_markout_removes_from_pending():
    ledger = make_ledger()
    record_fill(ledger, "f1", strategy="A", mid_at_fill=0.50)
    ledger.record_markout("f1", mid_at_t30=0.51)
    assert "f1" not in ledger.pending_markout_fill_ids()


def test_record_markout_unknown_fill_returns_none():
    ledger = make_ledger()
    result = ledger.record_markout("unknown", mid_at_t30=0.50)
    assert result is None


def test_all_fills_returns_all():
    ledger = make_ledger()
    record_fill(ledger, "f1")
    record_fill(ledger, "f2")
    assert len(ledger.all_fills()) == 2
