"""
Integration tests for book resync policy (§5.1.2).

Five cases:
1. Stable + periodic: normal resync with no escalation
2. Delta threshold: BOOK_RESYNC_DELTA_THRESHOLD missed deltas → resync_queue
3. Escalation — mid move > BOOK_RESYNC_CANCEL_MID_PCT → quotes cancelled
4. Escalation — spread > BOOK_RESYNC_CANCEL_SPREAD_TICKS → quotes cancelled
5. Escalation — gap > BOOK_RESYNC_CANCEL_GAP_MS → quotes cancelled
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from config.settings import Settings
from core.execution.book_state import BookStateStore
from core.execution.types import BookEvent, PriceLevel


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
        BOOK_RESYNC_CANCEL_MID_PCT=0.5,
        BOOK_RESYNC_CANCEL_SPREAD_TICKS=10,
        BOOK_RESYNC_CANCEL_GAP_MS=2000,
        BOOK_RESYNC_DELTA_THRESHOLD=5,
    )


def make_book_event(
    token_id: str = "tok_1",
    bid: float = 0.45,
    ask: float = 0.55,
) -> BookEvent:
    return BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=bid, size=100.0)],
        asks=[PriceLevel(price=ask, size=100.0)],
        timestamp=time.time(),
    )


# ── Case 1: Stable + periodic resync ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_stable_periodic_resync_no_escalation(settings):
    """Periodic resync when book is stable produces no escalation (no cancel needed)."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event(bid=0.45, ask=0.55))
    # Normal gap, stable mid
    escalate = await book.start_resync(ws_gap_ms=100, settings=settings)
    assert escalate is False
    assert book.resyncing is True


@pytest.mark.asyncio
async def test_complete_resync_clears_flag(settings):
    """complete_resync() clears resyncing flag and updates book from REST response."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event(bid=0.45, ask=0.55))
    await book.start_resync(ws_gap_ms=0, settings=settings)
    assert book.resyncing is True

    rest_snapshot = {
        "bids": [{"price": "0.46", "size": "50"}],
        "asks": [{"price": "0.54", "size": "50"}],
    }
    await book.complete_resync(rest_snapshot)

    assert book.resyncing is False
    assert book.best_bid() == pytest.approx(0.46)
    assert book.best_ask() == pytest.approx(0.54)


@pytest.mark.asyncio
async def test_complete_resync_resets_missed_delta_count(settings):
    """complete_resync() resets the missed delta counter."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event())
    book.missed_delta_count = 4
    await book.start_resync(ws_gap_ms=0, settings=settings)
    await book.complete_resync({"bids": [], "asks": []})
    assert book.missed_delta_count == 0


@pytest.mark.asyncio
async def test_complete_resync_updates_mid(settings):
    """After complete_resync(), last_mid reflects the REST snapshot."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event(bid=0.40, ask=0.60))
    await book.start_resync(ws_gap_ms=0, settings=settings)
    await book.complete_resync({
        "bids": [{"price": "0.48", "size": "10"}],
        "asks": [{"price": "0.52", "size": "10"}],
    })
    assert book.last_mid == pytest.approx(0.50)


# ── Case 2: Delta threshold ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delta_threshold_triggers_resync_queue():
    """BOOK_RESYNC_DELTA_THRESHOLD crossed-book messages → token in resync_queue."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(
        book_queue=book_queue,
        resync_queue=resync_queue,
        delta_threshold=5,
    )

    # A crossed book (bid >= ask) is treated as a missed delta
    crossed_msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok_1",
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],  # crossed: ask < bid
    })

    for _ in range(5):
        await gw._handle_message(crossed_msg)

    assert not resync_queue.empty()
    token = await resync_queue.get()
    assert token == "tok_1"


@pytest.mark.asyncio
async def test_delta_threshold_count_resets_after_trigger():
    """After threshold is reached and resync enqueued, miss count resets."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(
        book_queue=book_queue,
        resync_queue=resync_queue,
        delta_threshold=5,
    )

    crossed_msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok_1",
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    })

    for _ in range(5):
        await gw._handle_message(crossed_msg)

    # Counter must be reset to 0 after trigger
    assert gw._missed_delta_counts.get("tok_1", 0) == 0


@pytest.mark.asyncio
async def test_four_crossed_books_not_yet_resync():
    """4 crossed-book messages (< threshold 5) do not trigger resync_queue."""
    from core.execution.market_stream import MarketStreamGateway

    book_queue: asyncio.Queue = asyncio.Queue()
    resync_queue: asyncio.Queue = asyncio.Queue()
    gw = MarketStreamGateway(
        book_queue=book_queue,
        resync_queue=resync_queue,
        delta_threshold=5,
    )

    crossed_msg = json.dumps({
        "event_type": "book",
        "asset_id": "tok_1",
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    })

    for _ in range(4):
        await gw._handle_message(crossed_msg)

    assert resync_queue.empty()


# ── Case 3: Escalation — mid move ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_mid_move_triggers_cancel(settings):
    """Mid price movement > BOOK_RESYNC_CANCEL_MID_PCT → escalation (True)."""
    book = BookStateStore(token_id="tok_1")

    # First update establishes _prev_mid
    book.update(make_book_event(bid=0.45, ask=0.55))  # mid=0.50
    # Second update moves mid significantly
    book.update(make_book_event(bid=0.20, ask=0.30))  # mid=0.25 → move=50%

    # _prev_mid = 0.50, current_mid = 0.25 → move = |0.25-0.50|/0.50 × 100 = 50%
    # BOOK_RESYNC_CANCEL_MID_PCT = 0.5 → 50% > 0.5% → escalate
    escalate = await book.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is True


@pytest.mark.asyncio
async def test_no_escalation_on_small_mid_move(settings):
    """Small mid movement within threshold does not escalate."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event(bid=0.45, ask=0.55))  # mid=0.50
    book.update(make_book_event(bid=0.449, ask=0.551))  # mid≈0.50, <0.5% move

    escalate = await book.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is False


# ── Case 4: Escalation — spread width ────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_wide_spread_triggers_cancel(settings):
    """Spread > BOOK_RESYNC_CANCEL_SPREAD_TICKS (10) → escalation."""
    book = BookStateStore(token_id="tok_1")
    # bid=0.40, ask=0.51 → spread = 0.11 / 0.01 = 11 ticks > 10
    book.update(make_book_event(bid=0.40, ask=0.51))

    escalate = await book.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is True


@pytest.mark.asyncio
async def test_no_escalation_narrow_spread(settings):
    """Spread within threshold does not escalate."""
    book = BookStateStore(token_id="tok_1")
    # bid=0.45, ask=0.54 → spread = 0.09 / 0.01 = 9 ticks ≤ 10
    book.update(make_book_event(bid=0.45, ask=0.54))

    escalate = await book.start_resync(ws_gap_ms=0, settings=settings)
    assert escalate is False


# ── Case 5: Escalation — WS gap ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_large_gap_triggers_cancel(settings):
    """ws_gap_ms > BOOK_RESYNC_CANCEL_GAP_MS (2000) → escalation."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event())

    escalate = await book.start_resync(ws_gap_ms=3000, settings=settings)
    assert escalate is True


@pytest.mark.asyncio
async def test_no_escalation_small_gap(settings):
    """ws_gap_ms at or below threshold does not escalate."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event())

    escalate = await book.start_resync(ws_gap_ms=1999, settings=settings)
    assert escalate is False


@pytest.mark.asyncio
async def test_escalation_gap_exact_boundary(settings):
    """ws_gap_ms exactly equal to threshold does not escalate (strict >)."""
    book = BookStateStore(token_id="tok_1")
    book.update(make_book_event())

    escalate = await book.start_resync(ws_gap_ms=2000, settings=settings)
    assert escalate is False
