"""
Unit tests for core/execution/book_state.py.
"""

import time

import pytest

from config.settings import Settings
from core.execution.book_state import BookStateStore
from core.execution.types import BookEvent, PriceLevel


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


def make_event(token_id, bids, asks, ts=None) -> BookEvent:
    return BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
        timestamp=ts or time.time(),
    )


# ── update() ──────────────────────────────────────────────────────────────────

def test_update_sets_bids_and_asks():
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.48, 100), (0.47, 50)], asks=[(0.52, 80)])
    store.update(event)
    assert store.best_bid() == 0.48
    assert store.best_ask() == 0.52


def test_update_sorts_bids_descending():
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.45, 10), (0.48, 20), (0.47, 30)], asks=[(0.52, 5)])
    store.update(event)
    assert store.bids[0].price == 0.48
    assert store.bids[1].price == 0.47
    assert store.bids[2].price == 0.45


def test_update_sorts_asks_ascending():
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.48, 10)], asks=[(0.55, 5), (0.52, 10), (0.53, 8)])
    store.update(event)
    assert store.asks[0].price == 0.52
    assert store.asks[1].price == 0.53
    assert store.asks[2].price == 0.55


def test_update_sets_last_mid():
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.48, 10)], asks=[(0.52, 10)])
    store.update(event)
    assert store.last_mid == pytest.approx(0.50)


def test_mid_returns_none_when_book_empty():
    store = BookStateStore(token_id="t1")
    assert store.mid() is None


def test_spread_ticks():
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.48, 10)], asks=[(0.52, 10)])
    store.update(event)
    assert store.spread_ticks(tick_size=0.01) == 4


# ── start_resync() — escalation conditions ────────────────────────────────────

@pytest.mark.asyncio
async def test_start_resync_returns_false_stable_market():
    """No escalation on a stable market."""
    settings = make_settings(
        BOOK_RESYNC_CANCEL_MID_PCT=0.5,
        BOOK_RESYNC_CANCEL_SPREAD_TICKS=10,
        BOOK_RESYNC_CANCEL_GAP_MS=2000,
    )
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.49, 10)], asks=[(0.51, 10)])
    store.update(event)

    escalate = await store.start_resync(ws_gap_ms=500, settings=settings)
    assert escalate is False
    assert store.resyncing is True


@pytest.mark.asyncio
async def test_start_resync_escalates_on_gap_ms():
    """Escalate when WS gap exceeds BOOK_RESYNC_CANCEL_GAP_MS."""
    settings = make_settings(BOOK_RESYNC_CANCEL_GAP_MS=2000)
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.49, 10)], asks=[(0.51, 10)])
    store.update(event)

    escalate = await store.start_resync(ws_gap_ms=3000, settings=settings)
    assert escalate is True


@pytest.mark.asyncio
async def test_start_resync_escalates_on_spread_width():
    """Escalate when spread exceeds BOOK_RESYNC_CANCEL_SPREAD_TICKS (using 0.01 tick)."""
    settings = make_settings(
        BOOK_RESYNC_CANCEL_SPREAD_TICKS=5,
        BOOK_RESYNC_CANCEL_GAP_MS=99999,
        BOOK_RESYNC_CANCEL_MID_PCT=999.0,
    )
    store = BookStateStore(token_id="t1")
    # spread = (0.60 - 0.48) / 0.01 = 12 ticks > 5
    event = make_event("t1", bids=[(0.48, 10)], asks=[(0.60, 10)])
    store.update(event)

    escalate = await store.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is True


@pytest.mark.asyncio
async def test_start_resync_escalates_on_mid_move():
    """Escalate when mid has moved > BOOK_RESYNC_CANCEL_MID_PCT since last update."""
    settings = make_settings(
        BOOK_RESYNC_CANCEL_MID_PCT=0.5,
        BOOK_RESYNC_CANCEL_SPREAD_TICKS=9999,
        BOOK_RESYNC_CANCEL_GAP_MS=99999,
    )
    store = BookStateStore(token_id="t1")
    # Establish a last_mid of 0.50
    event1 = make_event("t1", bids=[(0.49, 10)], asks=[(0.51, 10)])
    store.update(event1)

    # Now the book has moved — mid is 0.55 (10% move, well above 0.5%)
    event2 = make_event("t1", bids=[(0.54, 10)], asks=[(0.56, 10)])
    store.update(event2)

    escalate = await store.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is True


# ── complete_resync() ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_resync_replaces_book_atomically():
    settings = make_settings()
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.40, 5)], asks=[(0.60, 5)])
    store.update(event)
    await store.start_resync(ws_gap_ms=0, settings=settings)
    assert store.resyncing is True

    rest_book = {
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.52", "size": "80"}],
    }
    await store.complete_resync(rest_book)

    assert store.resyncing is False
    assert store.best_bid() == pytest.approx(0.48)
    assert store.best_ask() == pytest.approx(0.52)
    assert store.missed_delta_count == 0


@pytest.mark.asyncio
async def test_complete_resync_clears_resyncing_flag():
    settings = make_settings()
    store = BookStateStore(token_id="t1")
    await store.start_resync(ws_gap_ms=0, settings=settings)
    assert store.resyncing is True

    await store.complete_resync({"bids": [], "asks": []})
    assert store.resyncing is False


@pytest.mark.asyncio
async def test_ack_during_resync_does_not_re_evaluate():
    """Confirmed state can be updated during resync; re-eval blocked until complete."""
    settings = make_settings()
    store = BookStateStore(token_id="t1")
    event = make_event("t1", bids=[(0.49, 10)], asks=[(0.51, 10)])
    store.update(event)
    await store.start_resync(ws_gap_ms=0, settings=settings)

    # While resyncing, an ack arrives — the book state is still 'resyncing'
    # The ExecutionActor checks store.resyncing before re-evaluating Desired vs Confirmed
    assert store.resyncing is True

    # Complete resync — now re-evaluation is permitted
    await store.complete_resync({"bids": [{"price": "0.49", "size": "10"}],
                                 "asks": [{"price": "0.51", "size": "10"}]})
    assert store.resyncing is False
