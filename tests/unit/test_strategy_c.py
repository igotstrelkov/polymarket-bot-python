"""
Unit tests for strategies/strategy_c.py.

Covers all entry gates, dynamic offset formula, size scaling, GTD Post-Only
expiry, YES and NO side signal routing, and FR-155 hard fee gate.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import PriceLevel
from inventory.manager import InventoryState
from strategies.strategy_c import StrategyC


# ── Helpers ───────────────────────────────────────────────────────────────────

_THREE_HOURS_S = 10_800
_FIVE_HOURS_S = 18_000


def make_market(
    *,
    accepting_orders: bool = True,
    resolution_time: datetime | None = None,
    neg_risk: bool = False,
    token_id: str = "tok1",
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond1",
        tick_size=0.01,
        minimum_order_size=1.0,
        neg_risk=neg_risk,
        fees_enabled=False,
        fee_rate_bps=0,
        seconds_delay=0,
        accepting_orders=accepting_orders,
        game_start_time=None,
        resolution_time=resolution_time,
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )


def make_book(bid: float = 0.91, ask: float = 0.95) -> BookStateStore:
    store = BookStateStore(token_id="tok1")
    store.bids = [PriceLevel(price=bid, size=100)]
    store.asks = [PriceLevel(price=ask, size=100)]
    store.last_mid = (bid + ask) / 2
    return store


def make_fee_cache(rate: int = 0) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = rate
    return cache


def make_inventory() -> InventoryState:
    return InventoryState(yes_shares=0.0, no_shares=0.0, yes_price=0.5)


def resolution_in(seconds: float) -> datetime:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc)


def default_strategy(**kwargs) -> StrategyC:
    defaults = dict(
        enabled=True,
        kill_switch_active=False,
        prob_threshold=0.90,
        max_fee_bps=5,
        snipe_min_size=5,
        snipe_max_size=20,
        resolution_warn_ms=7_200_000,   # 2h
        snipe_entry_window_ms=14_400_000,  # 4h
        gtd_resolution_buffer_ms=7_200_000,
    )
    defaults.update(kwargs)
    return StrategyC(**defaults)


# ── Kill switch / disabled ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_kill_switch():
    s = default_strategy(kill_switch_active=True)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_disabled():
    s = default_strategy(enabled=False)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate: negRisk ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_for_neg_risk_market():
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S), neg_risk=True),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate: accepting_orders ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_not_accepting_orders():
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S), accepting_orders=False),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate: FR-155 fee hard gate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_fee_equals_max_fee_bps():
    """fee=5, max_fee=5 → strict < fails → no entry."""
    s = default_strategy(max_fee_bps=5)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache(rate=5)
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_fee_above_max_fee_bps():
    s = default_strategy(max_fee_bps=5)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache(rate=6)
    )
    assert result == []


@pytest.mark.asyncio
async def test_proceeds_when_fee_below_max_fee_bps():
    s = default_strategy(max_fee_bps=5)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache(rate=4)
    )
    assert len(result) == 1


# ── Gate: resolution time window ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_within_inner_bound():
    """1h to resolution — within 2h inner bound → no entry."""
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(3_600)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_beyond_outer_bound():
    """5h to resolution — beyond 4h outer bound → no entry."""
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_FIVE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_no_resolution_time():
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=None), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_proceeds_within_entry_window():
    """3h to resolution — within 2h-4h window → entry allowed."""
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert len(result) == 1


# ── Gate: probability threshold ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_prob_not_above_threshold():
    """mid = 0.85 — below 0.90 threshold and above 0.10 (1-0.90) → no entry."""
    book = make_book(bid=0.83, ask=0.87)  # mid = 0.85
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_yes_side_when_prob_above_threshold():
    """mid = 0.93 → YES side signal."""
    book = make_book(bid=0.91, ask=0.95)  # mid = 0.93
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    assert result[0].side == "BUY"


@pytest.mark.asyncio
async def test_no_side_when_yes_prob_below_one_minus_threshold():
    """mid = 0.05 → NO side (YES < 0.10) → BUY signal."""
    book = make_book(bid=0.03, ask=0.07)  # mid = 0.05
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    assert result[0].side == "BUY"


# ── Gate: spread >= 2¢ ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_spread_too_narrow():
    """spread = 0.01 < 0.02 minimum."""
    book = make_book(bid=0.93, ask=0.94)  # spread = 0.01
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate: position limit ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_position_at_limit():
    s = default_strategy(max_exposure=50.0)
    s.current_position = 50.0
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Dynamic offset formula ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_offset_clamped_to_minimum_001():
    """spread = 0.01 → offset = max(0.01, min(0.02, 0.005)) = 0.01."""
    # But spread < 0.02 fails the spread gate, so use spread=0.02 exactly
    book = make_book(bid=0.91, ask=0.93)  # spread = 0.02; mid = 0.92
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    # offset = max(0.01, min(0.02, 0.02*0.5)) = max(0.01, 0.01) = 0.01
    expected_price = round(0.93 - 0.01, 4)
    assert result[0].price == pytest.approx(expected_price, abs=1e-4)


@pytest.mark.asyncio
async def test_offset_clamped_to_maximum_002():
    """spread = 0.10 → offset = max(0.01, min(0.02, 0.05)) = 0.02."""
    book = make_book(bid=0.90, ask=1.00)  # spread = 0.10; mid = 0.95
    # best_ask = 1.00 → clamp issue... let me use bid=0.91, ask=1.00 (not possible > 0.99)
    book = make_book(bid=0.91, ask=0.99)  # spread = 0.08; mid = 0.95
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    # spread = 0.08 → offset = max(0.01, min(0.02, 0.04)) = 0.02
    expected_price = round(0.99 - 0.02, 4)
    assert result[0].price == pytest.approx(expected_price, abs=1e-4)


# ── Size scaling ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_size_formula_at_threshold():
    """prob_certainty = 0.90 → scale = 0.0 → size = snipe_min_size."""
    # Mid just above 0.90: bid=0.89, ask=0.93 → mid=0.91
    book = make_book(bid=0.89, ask=0.93)  # mid = 0.91, spread = 0.04
    s = default_strategy(prob_threshold=0.90, snipe_min_size=5, snipe_max_size=20)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    # prob_certainty = 0.91, scale = (0.91 - 0.90) / 0.10 = 0.10
    # size = 5 + int(0.10 * 15) = 5 + 1 = 6
    assert result[0].size == 6


@pytest.mark.asyncio
async def test_size_formula_prd_example():
    """PRD example: threshold=0.90, YES_prob=0.95 → size=12."""
    # mid=0.95 → book: bid=0.93, ask=0.97 → mid=0.95, spread=0.04
    book = make_book(bid=0.93, ask=0.97)
    s = default_strategy(prob_threshold=0.90, snipe_min_size=5, snipe_max_size=20)
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert len(result) == 1
    # prob_certainty = 0.95, scale = (0.95-0.90)/0.10 = 0.5
    # size = 5 + floor(0.5 * 15) = 5 + 7 = 12
    assert result[0].size == 12


# ── Signal properties ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_is_post_only():
    book = make_book(bid=0.91, ask=0.95)
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert result[0].post_only is True


@pytest.mark.asyncio
async def test_signal_is_gtd():
    book = make_book(bid=0.91, ask=0.95)
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert result[0].time_in_force == "GTD"


@pytest.mark.asyncio
async def test_signal_strategy_label():
    book = make_book(bid=0.91, ask=0.95)
    s = default_strategy()
    result = await s.evaluate(
        make_market(resolution_time=resolution_in(_THREE_HOURS_S)),
        book, make_inventory(), make_fee_cache()
    )
    assert result[0].strategy == "C"


# ── GTD expiry formula ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gtd_expiry_formula():
    """expiration = resolution_unix - buffer_s + 60."""
    resolution_ts = time.time() + _THREE_HOURS_S
    res_dt = datetime.fromtimestamp(resolution_ts, tz=timezone.utc)
    buffer_ms = 7_200_000

    s = default_strategy(gtd_resolution_buffer_ms=buffer_ms)
    book = make_book(bid=0.91, ask=0.95)
    result = await s.evaluate(
        make_market(resolution_time=res_dt), book, make_inventory(), make_fee_cache()
    )
    expected = int(resolution_ts - buffer_ms // 1000 + 60)
    assert result[0].expiration == expected
