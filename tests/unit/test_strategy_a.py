"""
Unit tests for strategies/strategy_a.py.

Covers all 10 entry gates, quote positioning, inventory skew offset,
GTD/GTC duration logic, and the post-adjustment spread guard.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import PriceLevel
from inventory.manager import InventoryState
from strategies.strategy_a import StrategyA


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_market(
    *,
    accepting_orders: bool = True,
    seconds_delay: int = 0,
    resolution_time: datetime | None = None,
    game_start_time: datetime | None = None,
    tick_size: float = 0.01,
    neg_risk: bool = False,
    fee_rate_bps: int = 78,
    token_id: str = "tok1",
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond1",
        tick_size=tick_size,
        minimum_order_size=1.0,
        neg_risk=neg_risk,
        fees_enabled=True,
        fee_rate_bps=fee_rate_bps,
        seconds_delay=seconds_delay,
        accepting_orders=accepting_orders,
        game_start_time=game_start_time,
        resolution_time=resolution_time,
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )


def make_book(bid: float = 0.47, ask: float = 0.53) -> BookStateStore:
    store = BookStateStore(token_id="tok1")
    store.bids = [PriceLevel(price=bid, size=100)]
    store.asks = [PriceLevel(price=ask, size=100)]
    store.last_mid = (bid + ask) / 2
    return store


def make_fee_cache(rate: int | None = 78) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = rate
    return cache


def make_inventory(yes: float = 0.0, no: float = 0.0) -> InventoryState:
    return InventoryState(yes_shares=yes, no_shares=no, yes_price=0.5)


def make_strategy(**kwargs) -> StrategyA:
    defaults = dict(enabled=True, kill_switch_active=False)
    defaults.update(kwargs)
    return StrategyA(**defaults)


# ── Kill switch / disabled ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_kill_switch():
    s = make_strategy(kill_switch_active=True)
    result = await s.evaluate(make_market(), make_book(), make_inventory(), make_fee_cache())
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_disabled():
    s = make_strategy(enabled=False)
    result = await s.evaluate(make_market(), make_book(), make_inventory(), make_fee_cache())
    assert result == []


# ── Gate 8: accepting_orders ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_not_accepting_orders():
    s = make_strategy()
    result = await s.evaluate(
        make_market(accepting_orders=False), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate 9: seconds_delay ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_seconds_delay_nonzero():
    s = make_strategy()
    result = await s.evaluate(
        make_market(seconds_delay=3), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate 1/7: resolution window ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_within_resolution_warn_window():
    """Market resolving in 1 hour — within 2-hour no-entry window."""
    future_1h = datetime.fromtimestamp(time.time() + 3_600, tz=timezone.utc)
    s = make_strategy(resolution_warn_ms=7_200_000)
    result = await s.evaluate(
        make_market(resolution_time=future_1h), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


@pytest.mark.asyncio
async def test_proceeds_when_resolution_far_away():
    """Market resolving in 10 hours — outside 2-hour window."""
    future_10h = datetime.fromtimestamp(time.time() + 36_000, tz=timezone.utc)
    s = make_strategy(resolution_warn_ms=7_200_000)
    result = await s.evaluate(
        make_market(resolution_time=future_10h), make_book(), make_inventory(), make_fee_cache()
    )
    assert len(result) == 2


# ── Gate 10: sports market past game_start ────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_game_start_passed():
    """game_start_time in the past → platform has auto-cancelled; skip entry."""
    past = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc)
    s = make_strategy()
    result = await s.evaluate(
        make_market(game_start_time=past), make_book(), make_inventory(), make_fee_cache()
    )
    assert result == []


# ── Gate 5: position limit ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_position_at_limit():
    s = make_strategy(max_exposure=100.0, current_position=100.0)
    result = await s.evaluate(make_market(), make_book(), make_inventory(), make_fee_cache())
    assert result == []


# ── Fee cache miss ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_on_fee_cache_miss():
    s = make_strategy()
    result = await s.evaluate(make_market(), make_book(), make_inventory(), make_fee_cache(rate=None))
    assert result == []


# ── Gate 4: midpoint range ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_mid_too_low():
    """Mid = 0.03 < 0.05 → out of range."""
    book = make_book(bid=0.02, ask=0.04)
    s = make_strategy()
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache())
    assert result == []


@pytest.mark.asyncio
async def test_returns_empty_when_mid_too_high():
    """Mid = 0.97 > 0.95 → out of range."""
    book = make_book(bid=0.96, ask=0.98)
    s = make_strategy()
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache())
    assert result == []


# ── Gate 2: FR-153 min spread ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_spread_below_min():
    """Observed spread = 0.02 < BASE_SPREAD (0.04) → FR-153 fails."""
    book = make_book(bid=0.49, ask=0.51)  # spread = 0.02
    s = make_strategy(base_spread=0.04)
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache(rate=78))
    assert result == []


# ── Gate 3: FR-154 high-fee guard ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_fee_gt_100_and_spread_not_3x():
    """fee_rate_bps=150 → 3x = 0.045; observed spread=0.04 fails FR-154."""
    book = make_book(bid=0.48, ask=0.52)  # spread = 0.04
    s = make_strategy()
    result = await s.evaluate(
        make_market(), book, make_inventory(), make_fee_cache(rate=150)
    )
    assert result == []


# ── Gate: inventory halt ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_inventory_halted():
    """skew = 1.0 ≥ 0.80 halt threshold → no quote."""
    inventory = InventoryState(yes_shares=100.0, no_shares=0.0, yes_price=0.5)
    s = make_strategy(inventory_halt_threshold=0.80)
    result = await s.evaluate(
        make_market(), make_book(), inventory, make_fee_cache()
    )
    assert result == []


# ── Gate 6: post-adjustment spread guard ────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_empty_when_post_adjustment_spread_too_narrow():
    """bid=0.485, ask=0.515 → after improving: bid=0.495, ask=0.505 → spread=0.01 < 0.03."""
    book = make_book(bid=0.485, ask=0.515)  # observed spread = 0.03 (passes FR-153)
    s = make_strategy(base_spread=0.02)  # lower base_spread so FR-153 passes
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache())
    assert result == []


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_bid_and_ask_signals_when_all_gates_pass():
    """Standard happy-path: bid = best_bid + tick, ask = best_ask - tick."""
    book = make_book(bid=0.46, ask=0.56)  # spread = 0.10
    s = make_strategy()
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache())
    assert len(result) == 2
    sides = {sig.side for sig in result}
    assert sides == {"BUY", "SELL"}


@pytest.mark.asyncio
async def test_bid_is_best_bid_plus_tick():
    book = make_book(bid=0.46, ask=0.56)
    s = make_strategy()
    result = await s.evaluate(make_market(tick_size=0.01), book, make_inventory(), make_fee_cache())
    buy_sig = next(sig for sig in result if sig.side == "BUY")
    assert buy_sig.price == pytest.approx(0.47)


@pytest.mark.asyncio
async def test_ask_is_best_ask_minus_tick():
    book = make_book(bid=0.46, ask=0.56)
    s = make_strategy()
    result = await s.evaluate(make_market(tick_size=0.01), book, make_inventory(), make_fee_cache())
    sell_sig = next(sig for sig in result if sig.side == "SELL")
    assert sell_sig.price == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_signals_are_post_only():
    book = make_book(bid=0.46, ask=0.56)
    s = make_strategy()
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache())
    assert all(sig.post_only is True for sig in result)


@pytest.mark.asyncio
async def test_signals_carry_fee_rate_from_cache():
    book = make_book(bid=0.46, ask=0.56)
    s = make_strategy()
    result = await s.evaluate(make_market(), book, make_inventory(), make_fee_cache(rate=120))
    assert all(sig.fee_rate_bps == 120 for sig in result)


# ── GTD / GTC duration ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gtc_when_far_from_resolution():
    """10 hours to resolution — beyond gtd_within_ms (6h) → GTC."""
    future = datetime.fromtimestamp(time.time() + 36_000, tz=timezone.utc)
    s = make_strategy(gtd_within_ms=21_600_000)
    book = make_book(bid=0.46, ask=0.56)
    result = await s.evaluate(
        make_market(resolution_time=future), book, make_inventory(), make_fee_cache()
    )
    assert all(sig.time_in_force == "GTC" for sig in result)
    assert all(sig.expiration is None for sig in result)


@pytest.mark.asyncio
async def test_gtd_when_within_gtd_window():
    """3 hours to resolution — within gtd_within_ms (6h) → GTD."""
    future = datetime.fromtimestamp(time.time() + 10_800, tz=timezone.utc)
    s = make_strategy(
        gtd_within_ms=21_600_000,
        gtd_resolution_buffer_ms=7_200_000,
        resolution_warn_ms=7_200_000,
    )
    book = make_book(bid=0.46, ask=0.56)
    result = await s.evaluate(
        make_market(resolution_time=future), book, make_inventory(), make_fee_cache()
    )
    assert all(sig.time_in_force == "GTD" for sig in result)
    assert all(sig.expiration is not None for sig in result)


@pytest.mark.asyncio
async def test_gtd_expiry_formula():
    """expiration = resolution_unix - buffer_s + 60."""
    resolution_ts = time.time() + 10_800  # 3 hours from now
    future = datetime.fromtimestamp(resolution_ts, tz=timezone.utc)
    buffer_ms = 7_200_000

    s = make_strategy(
        gtd_within_ms=21_600_000,
        gtd_resolution_buffer_ms=buffer_ms,
        resolution_warn_ms=7_200_000,
    )
    book = make_book(bid=0.46, ask=0.56)
    result = await s.evaluate(
        make_market(resolution_time=future), book, make_inventory(), make_fee_cache()
    )
    expected_expiry = int(resolution_ts - buffer_ms // 1000 + 60)
    assert all(sig.expiration == expected_expiry for sig in result)


# ── Inventory skew offset ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skew_offset_applied_when_above_threshold():
    """skew > 0.65 → positive offset → bid lowered, ask raised."""
    # 100 YES at p=0.5 → skew = 1.0; offset = round(1.0 × 3) = 3 ticks
    inventory = InventoryState(yes_shares=100.0, no_shares=0.0, yes_price=0.5)
    book = make_book(bid=0.40, ask=0.60)  # wide spread so gate 6 doesn't fire
    s = make_strategy(
        inventory_skew_threshold=0.65,
        inventory_halt_threshold=1.1,  # above max possible skew so halt never fires
        inventory_skew_multiplier=3,
    )
    result = await s.evaluate(make_market(), book, inventory, make_fee_cache())
    assert len(result) == 2  # produced signals despite skew (halt not triggered)
    buy_sig = next(sig for sig in result if sig.side == "BUY")
    sell_sig = next(sig for sig in result if sig.side == "SELL")
    # Offset = 3 ticks; bid lowered by 3×0.01=0.03; ask raised by 3×0.01=0.03
    assert buy_sig.price < 0.40 + 0.01  # bid + tick - offset < best_bid + tick
    assert sell_sig.price > 0.60 - 0.01  # ask - tick + offset > best_ask - tick


@pytest.mark.asyncio
async def test_no_skew_offset_when_below_threshold():
    """skew = 0 → no offset applied."""
    inventory = make_inventory()  # 0 YES, 0 NO → skew = 0
    book = make_book(bid=0.46, ask=0.56)
    s = make_strategy(inventory_skew_threshold=0.65)
    result = await s.evaluate(make_market(tick_size=0.01), book, inventory, make_fee_cache())
    buy_sig = next(sig for sig in result if sig.side == "BUY")
    assert buy_sig.price == pytest.approx(0.47)
