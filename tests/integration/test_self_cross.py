"""
Integration tests for self-cross detection (FR-210a).

Own resting SELL@0.61, new BUY signal@0.60 (1 tick apart):
  → CancelMutation(SELL) appears before PlaceMutation(BUY) in the mutations list.

Additional cases:
- BUY resting, new SELL within 1 tick → cancel BUY first
- Orders more than 1 tick apart → no self-cross cancel
- Self-cross cancel labelled with reason="self_cross"
- Multiple opposing orders within range → all cancelled
"""

from __future__ import annotations

import pytest

from core.execution.execution_actor import (
    CancelMutation,
    ConfirmedOrder,
    PlaceMutation,
    diff,
)
from core.execution.types import OrderIntent


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_intent(
    token_id: str = "tok_1",
    side: str = "BUY",
    price: float = 0.60,
    size: float = 10.0,
    tick_size: float = 0.01,
    strategy: str = "A",
) -> OrderIntent:
    return OrderIntent(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy=strategy,
        fee_rate_bps=0,
        neg_risk=False,
        tick_size=tick_size,
    )


def make_confirmed(
    order_id: str = "ord_1",
    token_id: str = "tok_1",
    side: str = "SELL",
    price: float = 0.61,
    size: float = 10.0,
    strategy: str = "A",
) -> ConfirmedOrder:
    return ConfirmedOrder(
        order_id=order_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        time_in_force="GTC",
        post_only=True,
        strategy=strategy,
    )


# ── Core self-cross scenario ──────────────────────────────────────────────────

def test_self_cross_cancel_before_place():
    """Own SELL@0.61, new BUY@0.60 → CancelMutation appears before PlaceMutation."""
    resting_sell = make_confirmed(order_id="sell_1", side="SELL", price=0.61)
    buy_intent = make_intent(side="BUY", price=0.60)

    mutations = diff([buy_intent], [resting_sell])

    cancel_indices = [i for i, m in enumerate(mutations) if isinstance(m, CancelMutation)]
    place_indices  = [i for i, m in enumerate(mutations) if isinstance(m, PlaceMutation)]

    assert len(cancel_indices) >= 1, "Expected at least one CancelMutation"
    assert len(place_indices) >= 1, "Expected at least one PlaceMutation"
    assert cancel_indices[-1] < place_indices[0], (
        "All CancelMutations must precede PlaceMutations"
    )


def test_self_cross_cancel_targets_resting_sell():
    """The CancelMutation targets the resting SELL order_id."""
    resting_sell = make_confirmed(order_id="sell_1", side="SELL", price=0.61)
    buy_intent = make_intent(side="BUY", price=0.60)

    mutations = diff([buy_intent], [resting_sell])

    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert "sell_1" in cancel_ids


def test_self_cross_cancel_has_self_cross_reason():
    """CancelMutation reason is 'self_cross' when the opposing order is also desired.

    FR-210a fires when a resting order is matched to a desired intent (so it would
    normally be kept) but a new opposing order crosses it within 1 tick.

    Setup:
      desired  = [BUY@0.60 (new), SELL@0.61 (keep existing)]
      confirmed = [SELL@0.61]

    The SELL@0.61 matches the desired SELL intent → not cancelled in step 2.
    The new BUY@0.60 is within 1 tick of SELL@0.61 → self_cross cancel fired.
    """
    resting_sell = make_confirmed(order_id="sell_1", side="SELL", price=0.61)
    buy_intent = make_intent(side="BUY", price=0.60)
    keep_sell_intent = make_intent(side="SELL", price=0.61)

    mutations = diff([buy_intent, keep_sell_intent], [resting_sell])

    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert len(self_cross_cancels) == 1
    assert self_cross_cancels[0].order_id == "sell_1"


def test_self_cross_place_mutation_is_buy():
    """The PlaceMutation following the cancel is for the BUY intent."""
    resting_sell = make_confirmed(order_id="sell_1", side="SELL", price=0.61)
    buy_intent = make_intent(side="BUY", price=0.60)

    mutations = diff([buy_intent], [resting_sell])

    places = [m for m in mutations if isinstance(m, PlaceMutation)]
    assert len(places) == 1
    assert places[0].intent.side == "BUY"
    assert places[0].intent.price == pytest.approx(0.60)


# ── Reverse: resting BUY, new SELL ───────────────────────────────────────────

def test_self_cross_resting_buy_cancelled_by_new_sell():
    """Own BUY@0.40, new SELL@0.41 → CancelMutation(BUY) before PlaceMutation(SELL)."""
    resting_buy = make_confirmed(order_id="buy_1", side="BUY", price=0.40)
    sell_intent = make_intent(side="SELL", price=0.41)

    mutations = diff([sell_intent], [resting_buy])

    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert "buy_1" in cancel_ids

    cancel_indices = [i for i, m in enumerate(mutations) if isinstance(m, CancelMutation)]
    place_indices  = [i for i, m in enumerate(mutations) if isinstance(m, PlaceMutation)]
    assert cancel_indices[-1] < place_indices[0]


# ── No self-cross when prices are far apart ───────────────────────────────────

def test_no_self_cross_when_prices_outside_tick_range():
    """SELL@0.70, new BUY@0.40 (30 ticks apart) → no self-cross cancel."""
    resting_sell = make_confirmed(order_id="sell_far", side="SELL", price=0.70)
    buy_intent = make_intent(side="BUY", price=0.40)

    mutations = diff([buy_intent], [resting_sell])

    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert len(self_cross_cancels) == 0


def test_no_self_cross_when_same_side():
    """Two BUY orders: no self-cross between them."""
    resting_buy = make_confirmed(order_id="buy_1", side="BUY", price=0.45)
    buy_intent = make_intent(side="BUY", price=0.46)

    mutations = diff([buy_intent], [resting_buy])

    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert len(self_cross_cancels) == 0


# ── Boundary: exactly 1 tick apart ───────────────────────────────────────────

def test_self_cross_at_exactly_one_tick():
    """SELL@0.61, BUY@0.60: exactly 1 tick difference → self-cross fires."""
    resting_sell = make_confirmed(order_id="sell_1", side="SELL", price=0.61)
    buy_intent = make_intent(side="BUY", price=0.60, tick_size=0.01)

    mutations = diff([buy_intent], [resting_sell])

    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert "sell_1" in cancel_ids


def test_no_self_cross_two_ticks_apart():
    """SELL@0.62, BUY@0.60: 2 ticks apart → no self-cross."""
    resting_sell = make_confirmed(order_id="sell_2tick", side="SELL", price=0.62)
    buy_intent = make_intent(side="BUY", price=0.60, tick_size=0.01)

    mutations = diff([buy_intent], [resting_sell])

    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert len(self_cross_cancels) == 0


# ── Multiple opposing orders ──────────────────────────────────────────────────

def test_multiple_opposing_orders_within_range_all_cancelled():
    """SELLs within 1 tick of a new BUY get self_cross cancels; distant SELLs stay.

    Setup: all three SELLs are desired (would normally be kept), plus a new BUY@0.60.
      sell_a@0.60 → 0 ticks from BUY → self_cross cancel
      sell_b@0.61 → 1 tick from BUY  → self_cross cancel
      sell_far@0.65 → 5 ticks from BUY → no cancel; order stays resting
    """
    sell_a = make_confirmed(order_id="sell_a", side="SELL", price=0.60)
    sell_b = make_confirmed(order_id="sell_b", side="SELL", price=0.61)
    sell_far = make_confirmed(order_id="sell_far", side="SELL", price=0.65)

    buy_intent = make_intent(side="BUY", price=0.60, tick_size=0.01)
    # Keep all three SELLs as desired so they are matched (not cancelled in step 2)
    keep_sell_a   = make_intent(side="SELL", price=0.60)
    keep_sell_b   = make_intent(side="SELL", price=0.61)
    keep_sell_far = make_intent(side="SELL", price=0.65)

    mutations = diff(
        [buy_intent, keep_sell_a, keep_sell_b, keep_sell_far],
        [sell_a, sell_b, sell_far],
    )

    cancel_ids = {m.order_id for m in mutations if isinstance(m, CancelMutation)}
    self_cross_ids = {
        m.order_id for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    }

    assert "sell_a" in self_cross_ids
    assert "sell_b" in self_cross_ids
    assert "sell_far" not in cancel_ids


# ── Self-cross does not affect different tokens ───────────────────────────────

def test_self_cross_only_within_same_token():
    """Self-cross detection is per-token; different tokens are not affected."""
    resting_sell_other = make_confirmed(
        order_id="sell_other",
        token_id="tok_2",
        side="SELL",
        price=0.61,
    )
    buy_intent = make_intent(token_id="tok_1", side="BUY", price=0.60)

    mutations = diff([buy_intent], [resting_sell_other])

    self_cross_cancels = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert len(self_cross_cancels) == 0
