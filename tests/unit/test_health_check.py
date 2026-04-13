"""
Unit tests for scripts/health_check.py — status helper functions.
Database and wallet checks are integration-only; tested here is the
output formatting and status aggregation logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.health_check import _GREEN, _RED, _WARN, _status_line


# ── _status_line formatting ───────────────────────────────────────────────────

def test_status_line_green():
    line = _status_line("Redis", _GREEN, "ping OK")
    assert "GREEN" in line
    assert "Redis" in line
    assert "ping OK" in line
    assert "✓" in line


def test_status_line_warn():
    line = _status_line("Fills", _WARN, "no fills yet")
    assert "WARN" in line
    assert "⚠" in line


def test_status_line_red():
    line = _status_line("Postgres", _RED, "connection refused")
    assert "RED" in line
    assert "✗" in line


def test_status_line_includes_all_parts():
    line = _status_line("My Check", _GREEN, "all good")
    assert "My Check" in line
    assert "all good" in line


# ── Status constants are distinct strings ─────────────────────────────────────

def test_status_constants_are_distinct():
    assert _GREEN != _WARN
    assert _WARN != _RED
    assert _GREEN != _RED


# ── Aggregation logic (mirrors _run summary) ──────────────────────────────────

def _summarise(checks: list[tuple[str, str, str]]) -> tuple[int, int, int, bool]:
    """Replicate the summary logic from _run."""
    red   = sum(1 for _, s, _ in checks if s == _RED)
    warn  = sum(1 for _, s, _ in checks if s == _WARN)
    green = sum(1 for _, s, _ in checks if s == _GREEN)
    any_red = red > 0
    return green, warn, red, any_red


def test_all_green_is_healthy():
    checks = [
        ("Redis", _GREEN, "ok"),
        ("Postgres", _GREEN, "ok"),
    ]
    green, warn, red, any_red = _summarise(checks)
    assert green == 2
    assert warn == 0
    assert red == 0
    assert any_red is False


def test_single_red_makes_unhealthy():
    checks = [
        ("Redis", _GREEN, "ok"),
        ("Postgres", _RED, "refused"),
    ]
    _, _, red, any_red = _summarise(checks)
    assert red == 1
    assert any_red is True


def test_warn_only_not_unhealthy():
    checks = [
        ("Redis", _GREEN, "ok"),
        ("Orders", _WARN, "no orders yet"),
    ]
    _, warn, _, any_red = _summarise(checks)
    assert warn == 1
    assert any_red is False


def test_mixed_red_and_warn():
    checks = [
        ("Redis", _RED, "timeout"),
        ("Orders", _WARN, "stale"),
        ("Postgres", _GREEN, "ok"),
    ]
    green, warn, red, any_red = _summarise(checks)
    assert green == 1
    assert warn == 1
    assert red == 1
    assert any_red is True
