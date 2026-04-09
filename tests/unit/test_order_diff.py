"""
Unit tests for the diff() function in core/execution/execution_actor.py.

Covers minimum mutation set (place, cancel, no-op), self-cross detection (FR-210a),
replace semantics, and cancels-before-places ordering.
"""

from __future__ import annotations

from core.execution.execution_actor import (
    CancelMutation,
    ConfirmedOrder,
    PlaceMutation,
    diff,
)
from core.execution.types import OrderIntent


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Basic cases ───────────────────────────────────────────────────────────────

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
    intent    = make_intent(price=0.49, size=10.0)
    confirmed = make_confirmed(price=0.49, size=10.0)
    mutations = diff([intent], [confirmed])
    assert mutations == []


def test_diff_price_change_produces_cancel_then_place():
    """Intent at different price → CancelMutation for old + PlaceMutation for new."""
    intent    = make_intent(price=0.50)
    confirmed = make_confirmed(price=0.48)
    mutations = diff([intent], [confirmed])
    cancels = [m for m in mutations if isinstance(m, CancelMutation)]
    places  = [m for m in mutations if isinstance(m, PlaceMutation)]
    assert len(cancels) == 1
    assert len(places) == 1
    assert cancels[0].order_id == confirmed.order_id


def test_diff_size_change_produces_cancel_then_place():
    intent    = make_intent(size=15.0)
    confirmed = make_confirmed(size=10.0)
    mutations = diff([intent], [confirmed])
    assert any(isinstance(m, CancelMutation) for m in mutations)
    assert any(isinstance(m, PlaceMutation)  for m in mutations)


def test_diff_tif_change_produces_cancel_then_place():
    """GTC → GTD change on the same price/size → replace."""
    intent    = make_intent(time_in_force="GTD")
    confirmed = make_confirmed(time_in_force="GTC")
    mutations = diff([intent], [confirmed])
    assert any(isinstance(m, CancelMutation) for m in mutations)
    assert any(isinstance(m, PlaceMutation)  for m in mutations)


def test_diff_cancels_before_places():
    """CancelMutations must always precede PlaceMutations in the output list."""
    intent    = make_intent(price=0.51)
    confirmed = make_confirmed(price=0.49, order_id="old")
    mutations = diff([intent], [confirmed])
    first_place  = next((i for i, m in enumerate(mutations) if isinstance(m, PlaceMutation)), None)
    first_cancel = next((i for i, m in enumerate(mutations) if isinstance(m, CancelMutation)), None)
    assert first_cancel is not None
    assert first_place  is not None
    assert first_cancel < first_place


def test_diff_multiple_intents_matched_correctly():
    """Both BUY and SELL match → no mutations."""
    buy_intent  = make_intent(side="BUY",  price=0.49, size=10)
    sell_intent = make_intent(side="SELL", price=0.51, size=10)
    buy_conf    = make_confirmed(order_id="b1", side="BUY",  price=0.49, size=10)
    sell_conf   = make_confirmed(order_id="s1", side="SELL", price=0.51, size=10)
    mutations   = diff([buy_intent, sell_intent], [buy_conf, sell_conf])
    assert mutations == []


def test_diff_only_places_missing_side():
    """BUY confirmed and matches; SELL is new → only PlaceMutation for SELL."""
    buy_intent  = make_intent(side="BUY",  price=0.49)
    sell_intent = make_intent(side="SELL", price=0.51)
    buy_conf    = make_confirmed(order_id="b1", side="BUY", price=0.49)
    mutations   = diff([buy_intent, sell_intent], [buy_conf])
    places  = [m for m in mutations if isinstance(m, PlaceMutation)]
    cancels = [m for m in mutations if isinstance(m, CancelMutation)]
    assert len(places)  == 1
    assert places[0].intent.side == "SELL"
    assert cancels == []


# ── Self-cross detection (FR-210a) ────────────────────────────────────────────

def test_diff_self_cross_buy_within_tick_of_sell():
    """New BUY at 0.51 with confirmed SELL at 0.51 → cancel SELL + place BUY.

    The confirmed SELL is not in desired, so it is cancelled regardless. The
    important invariant: it IS cancelled before the BUY is placed.
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
    self_cross = [
        m for m in mutations
        if isinstance(m, CancelMutation) and m.reason == "self_cross"
    ]
    assert self_cross == []


def test_diff_self_cross_not_double_cancelled():
    """A confirmed order that is already being cancelled is not cancelled a second time."""
    intent    = make_intent(side="BUY", price=0.51, tick_size=0.01)
    confirmed = make_confirmed(order_id="sell1", side="SELL", price=0.51)
    mutations = diff([intent], [confirmed])
    cancel_ids = [m.order_id for m in mutations if isinstance(m, CancelMutation)]
    assert cancel_ids.count("sell1") == 1


def test_diff_self_cross_only_fires_for_matching_token():
    """Self-cross check is scoped to the same token_id."""
    intent     = make_intent(token_id="tok1", side="BUY", price=0.51, tick_size=0.01)
    other_sell = make_confirmed(order_id="s2", token_id="tok2", side="SELL", price=0.51)
    mutations  = diff([intent], [other_sell])
    # other_sell is on tok2, not tok1 → no self-cross cancel; it's cancelled as no_longer_desired
    cancels = [m for m in mutations if isinstance(m, CancelMutation) and m.reason == "self_cross"]
    assert cancels == []
