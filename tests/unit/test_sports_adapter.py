"""
Unit tests for core/control/sports_adapter.py.

Covers is_sports_market, should_cancel_at_game_start,
compute_gtd_before_game_start, and marketable_order_delay_ms.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from core.control.sports_adapter import (
    compute_gtd_before_game_start,
    is_sports_market,
    marketable_order_delay_ms,
    should_cancel_at_game_start,
)


# ── is_sports_market ──────────────────────────────────────────────────────────

def test_is_sports_market_false_when_no_game_start():
    assert is_sports_market(None) is False


def test_is_sports_market_true_when_game_start_set():
    gst = datetime.fromtimestamp(time.time() + 3_600, tz=timezone.utc)
    assert is_sports_market(gst) is True


def test_is_sports_market_true_even_when_game_start_past():
    """A market with a past game_start_time is still a sports market."""
    gst = datetime.fromtimestamp(time.time() - 3_600, tz=timezone.utc)
    assert is_sports_market(gst) is True


# ── should_cancel_at_game_start ───────────────────────────────────────────────

def test_cancel_false_when_game_start_none():
    assert should_cancel_at_game_start(None) is False


def test_cancel_false_when_game_start_in_future():
    future = datetime.fromtimestamp(time.time() + 3_600, tz=timezone.utc)
    assert should_cancel_at_game_start(future) is False


def test_cancel_true_when_game_start_in_past():
    past = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc)
    assert should_cancel_at_game_start(past) is True


def test_cancel_true_when_game_start_exactly_now():
    """At game_start_time exactly (<=), cancel should fire."""
    now_ms = time.time() * 1000
    # Game start is at exactly now_ms
    game_start = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    # Pass the same now_ms explicitly so there's no race
    assert should_cancel_at_game_start(game_start, now_ms=now_ms) is True


def test_cancel_uses_explicit_now_ms():
    """Verify the now_ms override is respected."""
    future = datetime.fromtimestamp(time.time() + 3_600, tz=timezone.utc)
    future_ms = future.timestamp() * 1000
    # Pass now_ms that is AFTER the game start
    assert should_cancel_at_game_start(future, now_ms=future_ms + 1_000) is True
    # Pass now_ms that is BEFORE the game start
    assert should_cancel_at_game_start(future, now_ms=future_ms - 1_000) is False


# ── compute_gtd_before_game_start ────────────────────────────────────────────

def test_gtd_formula_default_buffer():
    """expiry = game_start_unix - (300_000 // 1000) + 60 = game_start_unix - 240."""
    game_start_ts = time.time() + 7_200
    gst = datetime.fromtimestamp(game_start_ts, tz=timezone.utc)
    result = compute_gtd_before_game_start(gst)
    expected = int(game_start_ts - 300 + 60)
    assert result == expected


def test_gtd_formula_custom_buffer():
    """GTD buffer = 600_000ms (10 min) → expiry = game_start_unix - 600 + 60."""
    game_start_ts = time.time() + 7_200
    gst = datetime.fromtimestamp(game_start_ts, tz=timezone.utc)
    result = compute_gtd_before_game_start(gst, gtd_game_start_buffer_ms=600_000)
    expected = int(game_start_ts - 600 + 60)
    assert result == expected


def test_gtd_expiry_is_before_game_start():
    """The resulting expiry must be strictly before game_start_time."""
    game_start_ts = time.time() + 7_200
    gst = datetime.fromtimestamp(game_start_ts, tz=timezone.utc)
    result = compute_gtd_before_game_start(gst)
    assert result < int(game_start_ts)


def test_gtd_expiry_includes_platform_60s_correction():
    """The +60 correction for the platform's 1-minute security threshold is applied."""
    game_start_ts = 1_800_000_000.0
    gst = datetime.fromtimestamp(game_start_ts, tz=timezone.utc)
    buffer_ms = 300_000
    result = compute_gtd_before_game_start(gst, gtd_game_start_buffer_ms=buffer_ms)
    # Without +60: game_start_ts - 300 = 1_799_999_700
    # With +60:    game_start_ts - 300 + 60 = 1_799_999_760
    assert result == int(game_start_ts - 300 + 60)


# ── marketable_order_delay_ms ────────────────────────────────────────────────

def test_marketable_order_delay_is_3000ms():
    """FR-119: sports markets carry a 3-second delay for marketable orders."""
    assert marketable_order_delay_ms() == 3_000
