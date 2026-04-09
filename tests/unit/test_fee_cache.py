"""
Unit tests for fees/cache.py.

Covers TTL expiry, miss tracking, deviation detection, on_fill re-fetch,
and fetch_fee_rate endpoint contract.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fees.cache import FeeRateCache, fetch_fee_rate


# ── TTL expiry ────────────────────────────────────────────────────────────────

def test_get_returns_value_within_ttl():
    cache = FeeRateCache(ttl_s=60)
    cache.set("tok1", 78)
    assert cache.get("tok1") == 78


def test_get_returns_none_after_ttl_expires():
    cache = FeeRateCache(ttl_s=1)
    cache.set("tok1", 78)
    with patch("fees.cache.time.monotonic", return_value=time.monotonic() + 2):
        assert cache.get("tok1") is None


def test_get_returns_none_for_unknown_token():
    cache = FeeRateCache()
    assert cache.get("unknown") is None


def test_invalidate_removes_entry():
    cache = FeeRateCache(ttl_s=60)
    cache.set("tok1", 78)
    cache.invalidate("tok1")
    assert cache.get("tok1") is None


def test_set_resets_miss_counter():
    cache = FeeRateCache(consecutive_miss_threshold=3)
    cache.record_miss("tok1")
    cache.record_miss("tok1")
    cache.set("tok1", 78)
    assert cache.should_exclude("tok1") is False


# ── Miss tracking ─────────────────────────────────────────────────────────────

def test_five_consecutive_misses_triggers_exclude():
    cache = FeeRateCache(consecutive_miss_threshold=5)
    for _ in range(5):
        cache.record_miss("tok1")
    assert cache.should_exclude("tok1") is True


def test_four_misses_not_yet_excluded():
    cache = FeeRateCache(consecutive_miss_threshold=5)
    for _ in range(4):
        cache.record_miss("tok1")
    assert cache.should_exclude("tok1") is False


def test_should_exclude_false_for_unknown_token():
    cache = FeeRateCache()
    assert cache.should_exclude("never_seen") is False


# ── Deviation detection ───────────────────────────────────────────────────────

def test_check_deviation_true_when_exceeds_threshold():
    """New value >10% higher than cached → deviation detected."""
    cache = FeeRateCache(ttl_s=60, deviation_threshold_pct=10.0)
    cache.set("tok1", 100)
    assert cache.check_deviation("tok1", 112) is True


def test_check_deviation_invalidates_cache_on_detection():
    """Cache entry is removed when deviation is detected."""
    cache = FeeRateCache(ttl_s=60, deviation_threshold_pct=10.0)
    cache.set("tok1", 100)
    cache.check_deviation("tok1", 115)
    assert cache.get("tok1") is None


def test_check_deviation_false_within_threshold():
    cache = FeeRateCache(ttl_s=60, deviation_threshold_pct=10.0)
    cache.set("tok1", 100)
    assert cache.check_deviation("tok1", 109) is False


def test_check_deviation_false_exactly_at_threshold():
    """Exactly 10% change does NOT trigger deviation (strictly greater-than)."""
    cache = FeeRateCache(ttl_s=60, deviation_threshold_pct=10.0)
    cache.set("tok1", 100)
    assert cache.check_deviation("tok1", 110) is False


def test_check_deviation_false_when_no_cached_value():
    cache = FeeRateCache(ttl_s=60, deviation_threshold_pct=10.0)
    assert cache.check_deviation("tok1", 100) is False


# ── on_fill() re-fetch ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_fill_triggers_refetch():
    """on_fill() calls the registered re-fetch function and updates cache."""
    cache = FeeRateCache(ttl_s=60)
    refetch_mock = AsyncMock(return_value=90)
    cache.set_refetch_fn(refetch_mock)

    await cache.on_fill("tok1")

    refetch_mock.assert_awaited_once_with("tok1")
    assert cache.get("tok1") == 90


@pytest.mark.asyncio
async def test_on_fill_noop_when_no_refetch_fn():
    """on_fill() silently does nothing if no refetch function is registered."""
    cache = FeeRateCache(ttl_s=60)
    # Should not raise
    await cache.on_fill("tok1")


@pytest.mark.asyncio
async def test_on_fill_handles_refetch_exception_gracefully():
    """Refetch failure is logged but does not raise."""
    cache = FeeRateCache(ttl_s=60)
    refetch_mock = AsyncMock(side_effect=ConnectionError("timeout"))
    cache.set_refetch_fn(refetch_mock)

    await cache.on_fill("tok1")  # Should not raise
    refetch_mock.assert_awaited_once_with("tok1")


# ── fetch_fee_rate — endpoint contract ───────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_fee_rate_reads_base_fee_field():
    """Response field is 'base_fee', NOT 'feeRateBps'."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"base_fee": 78, "feeRateBps": 999}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    result = await fetch_fee_rate(
        token_id="tok1",
        http_client=mock_client,
        clob_host="https://clob.polymarket.com",
        hmac_headers={"Authorization": "hmac"},
    )

    assert result == 78
    mock_client.get.assert_awaited_once_with(
        "https://clob.polymarket.com/fee-rate/tok1",
        headers={"Authorization": "hmac"},
    )


@pytest.mark.asyncio
async def test_fetch_fee_rate_raises_on_http_error():
    """raise_for_status() propagates HTTP errors."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("404 Not Found")

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    with pytest.raises(Exception, match="404"):
        await fetch_fee_rate("tok1", mock_client, "https://clob.polymarket.com", {})
