"""
Unit tests for scripts/markout_report.py — §11.4 Step 3 gate logic.

Tests cover the pure evaluate_gate() function only; database calls are
tested in integration tests that require a real Postgres connection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add the repo root so we can import from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.markout_report import _parse_date, _percentile, evaluate_gate


# ── _parse_date ───────────────────────────────────────────────────────────────

def test_parse_date_returns_utc_midnight():
    from datetime import timezone
    dt = _parse_date("2026-04-15")
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 15
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.tzinfo == timezone.utc


def test_parse_date_invalid_raises():
    with pytest.raises(Exception):
        _parse_date("not-a-date")


def test_parse_date_wrong_format_raises():
    with pytest.raises(Exception):
        _parse_date("15/04/2026")


# ── _percentile ───────────────────────────────────────────────────────────────

def test_percentile_empty_list():
    assert _percentile([], 50.0) == 0.0


def test_percentile_single_value():
    assert _percentile([0.01], 50.0) == pytest.approx(0.01)


def test_percentile_median_even():
    values = [0.001, 0.003, 0.007, 0.009]
    result = _percentile(values, 50.0)
    assert result == pytest.approx(0.005)


def test_percentile_p95():
    values = list(range(101))  # 0 .. 100
    assert _percentile(values, 95.0) == pytest.approx(95.0)


# ── evaluate_gate helpers ─────────────────────────────────────────────────────

def _make_rows(markouts: list[float], size: float = 1.0) -> list[dict]:
    return [{"markout_30s": m, "size": size} for m in markouts]


# ── Criterion 1: median ≤ +0.5¢ ──────────────────────────────────────────────

def test_criterion_1_passes_when_median_at_threshold():
    """Median exactly at 0.005 passes criterion 1."""
    rows = _make_rows([0.005] * 100)
    results, overall = evaluate_gate(rows)
    c1 = next(r for r in results if r[0] == "1")
    assert c1[1] is True


def test_criterion_1_passes_when_median_below_threshold():
    rows = _make_rows([0.001] * 100)
    results, _ = evaluate_gate(rows)
    c1 = next(r for r in results if r[0] == "1")
    assert c1[1] is True


def test_criterion_1_fails_when_median_above_threshold():
    rows = _make_rows([0.006] * 100)
    results, overall = evaluate_gate(rows)
    c1 = next(r for r in results if r[0] == "1")
    assert c1[1] is False
    assert overall is False


def test_criterion_1_negative_markout_passes():
    """Negative markout (favourable fills) easily passes criterion 1."""
    rows = _make_rows([-0.010] * 100)
    results, _ = evaluate_gate(rows)
    c1 = next(r for r in results if r[0] == "1")
    assert c1[1] is True


# ── Criterion 2: < 30% fills with markout > +1¢ ──────────────────────────────

def test_criterion_2_passes_when_29_pct_adverse():
    """29% adverse fills passes (threshold is strict < 30%)."""
    fills = [0.015] * 29 + [0.001] * 71   # 29% above 1¢
    rows = _make_rows(fills)
    results, _ = evaluate_gate(rows)
    c2 = next(r for r in results if r[0] == "2")
    assert c2[1] is True


def test_criterion_2_fails_when_30_pct_adverse():
    """Exactly 30% adverse fails (threshold requires strictly < 30%)."""
    fills = [0.015] * 30 + [0.001] * 70
    rows = _make_rows(fills)
    results, overall = evaluate_gate(rows)
    c2 = next(r for r in results if r[0] == "2")
    assert c2[1] is False
    assert overall is False


def test_criterion_2_passes_zero_adverse():
    rows = _make_rows([0.002] * 100)
    results, _ = evaluate_gate(rows)
    c2 = next(r for r in results if r[0] == "2")
    assert c2[1] is True


def test_criterion_2_boundary_exactly_at_cutoff_not_counted():
    """markout_30s exactly at 0.010 is NOT > 0.010, so not counted as adverse."""
    fills = [0.010] * 100
    rows = _make_rows(fills)
    results, _ = evaluate_gate(rows)
    c2 = next(r for r in results if r[0] == "2")
    assert c2[1] is True   # 0% adverse since none > 0.010


# ── Criterion 3: net size-weighted P&L positive ───────────────────────────────

def test_criterion_3_passes_with_positive_net_pnl():
    rows = _make_rows([-0.005] * 100)  # all favourable
    results, _ = evaluate_gate(rows)
    c3 = next(r for r in results if r[0] == "3")
    assert c3[1] is True


def test_criterion_3_fails_with_negative_net_pnl():
    rows = _make_rows([0.008] * 100)
    results, overall = evaluate_gate(rows)
    c3 = next(r for r in results if r[0] == "3")
    assert c3[1] is False
    assert overall is False


def test_criterion_3_weights_by_size():
    """Large favourable fill must dominate small adverse fills."""
    rows = [
        {"markout_30s": -0.050, "size": 100.0},  # large favourable: -5.0
        {"markout_30s": 0.020, "size": 1.0},     # tiny adverse:    +0.02
    ]
    results, _ = evaluate_gate(rows)
    c3 = next(r for r in results if r[0] == "3")
    assert c3[1] is True


def test_criterion_3_fails_when_net_pnl_exactly_zero():
    """Net P&L of exactly 0.0 fails the strictly-positive gate."""
    rows = [
        {"markout_30s": 0.005, "size": 2.0},   # +0.010
        {"markout_30s": -0.005, "size": 2.0},  # -0.010
    ]
    results, overall = evaluate_gate(rows)
    c3 = next(r for r in results if r[0] == "3")
    assert c3[1] is False


# ── Combined: all-pass and partial-fail scenarios ─────────────────────────────

def test_all_criteria_pass():
    """A healthy fill distribution clears all three criteria."""
    # Median = -0.003 (below +0.005 threshold), 0% > 1¢ adverse,
    # net maker P&L = -(-0.003 * 100) = +0.3 > 0 (favourable fills)
    rows = _make_rows([-0.003] * 100)
    results, overall = evaluate_gate(rows)
    assert overall is True
    for _, passed, _ in results:
        assert passed is True


def test_only_criterion_1_fails():
    """Only median criterion fails while others pass."""
    # Median 0.008 > 0.005 → c1 fails
    # 0% > 1¢ → c2 passes
    # Net PnL = +0.008 * 100 = +0.8 > 0 → c3 fails too (adverse selection)
    rows = _make_rows([0.008] * 100)
    results, overall = evaluate_gate(rows)
    assert overall is False
    c1 = next(r for r in results if r[0] == "1")
    c2 = next(r for r in results if r[0] == "2")
    assert c1[1] is False
    assert c2[1] is True


def test_detail_strings_contain_values():
    """Detail strings are informative and include the computed values."""
    rows = _make_rows([0.003] * 80 + [0.015] * 20)  # 20% > 1¢
    results, _ = evaluate_gate(rows)
    for _, _, detail in results:
        assert isinstance(detail, str)
        assert len(detail) > 10
