"""
Integration tests for fee cache outage behaviour.

FR-156: After FEE_CONSECUTIVE_MISS_THRESHOLD consecutive misses the token must
be excluded from quoting and an alert sent. When the cache warms again the
token re-enters on the next scan cycle.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from fees.cache import FeeRateCache


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_cache(miss_threshold: int = 5) -> FeeRateCache:
    return FeeRateCache(
        ttl_s=30,
        consecutive_miss_threshold=miss_threshold,
        deviation_threshold_pct=10.0,
    )


# ── Test 1: 5 consecutive misses → market excluded ────────────────────────────

def test_four_misses_not_yet_excluded():
    """Fewer than threshold misses do not exclude the market."""
    cache = make_cache(miss_threshold=5)
    for _ in range(4):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is False


def test_five_misses_triggers_exclusion():
    """Exactly 5 consecutive misses ≥ threshold → should_exclude returns True."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is True


def test_six_misses_still_excluded():
    """Misses above threshold also excluded."""
    cache = make_cache(miss_threshold=5)
    for _ in range(6):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is True


def test_exclusion_is_per_token():
    """Misses on tok_1 do not affect tok_2."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is True
    assert cache.should_exclude("tok_2") is False


def test_no_misses_not_excluded():
    """Token with zero recorded misses is not excluded."""
    cache = make_cache(miss_threshold=5)
    assert cache.should_exclude("tok_1") is False


# ── Test 2: alert sent after threshold reached ────────────────────────────────

@pytest.mark.asyncio
async def test_alert_dispatched_when_threshold_reached():
    """A scanner-like loop must call alerter when should_exclude() transitions to True."""
    cache = make_cache(miss_threshold=5)
    mock_alerter = AsyncMock()

    alerted = False
    was_excluded_before = False

    for _ in range(5):
        cache.record_miss("tok_1")
        if cache.should_exclude("tok_1") and not was_excluded_before:
            await mock_alerter.send("FEE_CACHE_SUSTAINED_OUTAGE", token_id="tok_1")
            alerted = True
            was_excluded_before = True

    assert alerted is True
    mock_alerter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_not_sent_before_threshold():
    """No alert is dispatched before the miss threshold is reached."""
    cache = make_cache(miss_threshold=5)
    mock_alerter = AsyncMock()

    for _ in range(4):
        cache.record_miss("tok_1")
        if cache.should_exclude("tok_1"):
            await mock_alerter.send("FEE_CACHE_SUSTAINED_OUTAGE", token_id="tok_1")

    mock_alerter.send.assert_not_awaited()


# ── Test 3: cache warms → market re-enters ────────────────────────────────────

def test_set_resets_miss_count():
    """Calling set() after misses resets the consecutive miss counter."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is True

    # Cache warms with a new fee rate
    cache.set("tok_1", 78)

    assert cache.should_exclude("tok_1") is False


def test_re_entry_after_cache_warm():
    """Market re-enters after the cache warms; get() returns the new value."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")

    cache.set("tok_1", 100)

    assert cache.should_exclude("tok_1") is False
    assert cache.get("tok_1") == 100


def test_partial_miss_count_preserved_until_warm():
    """After 3 misses, the token is not yet excluded; set() resets to 0."""
    cache = make_cache(miss_threshold=5)
    for _ in range(3):
        cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is False

    cache.set("tok_1", 50)
    # One more miss after warming — should not re-trigger exclusion
    cache.record_miss("tok_1")
    assert cache.should_exclude("tok_1") is False


def test_market_excluded_in_scan_cycle():
    """Simulated scan cycle: market is skipped when should_exclude() is True."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")

    # Simulated scan cycle
    quotes_issued: list[str] = []
    for token_id in ["tok_1", "tok_2"]:
        if cache.should_exclude(token_id):
            continue  # skip excluded market
        quotes_issued.append(token_id)

    assert "tok_1" not in quotes_issued
    assert "tok_2" in quotes_issued


def test_market_re_enters_after_cache_warm_in_scan_cycle():
    """After cache warms, the market re-enters on the next scan cycle."""
    cache = make_cache(miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok_1")

    assert cache.should_exclude("tok_1") is True

    # Cache warms
    cache.set("tok_1", 78)

    # Next scan cycle
    quotes_issued: list[str] = []
    for token_id in ["tok_1"]:
        if not cache.should_exclude(token_id):
            quotes_issued.append(token_id)

    assert "tok_1" in quotes_issued
