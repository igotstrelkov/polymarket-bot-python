"""
Unit tests for metrics/prometheus.py.

Covers: LatencyTracker P95, MetricsStore update/read, drawdown tracking,
midnight reset, and snapshot dict keys (FR-605).
"""

from __future__ import annotations

import pytest

from metrics.prometheus import LatencyTracker, MetricsStore


# ── LatencyTracker ────────────────────────────────────────────────────────────

def test_latency_tracker_p95_empty():
    tracker = LatencyTracker()
    assert tracker.p95() == pytest.approx(0.0)


def test_latency_tracker_single_sample():
    tracker = LatencyTracker()
    tracker.record(42.0)
    assert tracker.p95() == pytest.approx(42.0)


def test_latency_tracker_p95_correct():
    tracker = LatencyTracker()
    for v in range(1, 101):   # 1..100
        tracker.record(float(v))
    # P95 of 1..100: ceil(0.95 * 100) = 95th value = 95.0
    assert tracker.p95() == pytest.approx(95.0)


def test_latency_tracker_count():
    tracker = LatencyTracker()
    tracker.record(10.0)
    tracker.record(20.0)
    assert tracker.count() == 2


def test_latency_tracker_maxlen_evicts_oldest():
    tracker = LatencyTracker(maxlen=3)
    for v in [1.0, 2.0, 3.0, 4.0]:
        tracker.record(v)
    # Oldest (1.0) evicted; remaining 2, 3, 4
    assert tracker.count() == 3
    assert tracker.p95() == pytest.approx(4.0)


# ── MetricsStore ──────────────────────────────────────────────────────────────

def test_metrics_store_initial_pnl_zero():
    store = MetricsStore()
    assert store.pnl_daily() == pytest.approx(0.0)


def test_set_pnl_daily():
    store = MetricsStore()
    store.set_pnl_daily(12.50)
    assert store.pnl_daily() == pytest.approx(12.50)


def test_reset_pnl_daily():
    store = MetricsStore()
    store.set_pnl_daily(99.0)
    store.reset_pnl_daily()
    assert store.pnl_daily() == pytest.approx(0.0)


def test_inc_trades():
    store = MetricsStore()
    store.inc_trades(3)
    store.inc_trades(2)
    assert store.trades_total() == 5


def test_observe_latency_recorded():
    store = MetricsStore()
    store.observe_latency(50.0)
    assert store.latency_p95() == pytest.approx(50.0)


def test_set_maker_ratio():
    store = MetricsStore()
    store.set_maker_ratio(0.95)
    assert store.maker_ratio() == pytest.approx(0.95)


def test_set_exposure():
    store = MetricsStore()
    store.set_exposure(1500.0)
    assert store.exposure_total() == pytest.approx(1500.0)


# ── Drawdown tracking ─────────────────────────────────────────────────────────

def test_drawdown_zero_initially():
    store = MetricsStore()
    assert store.drawdown() == pytest.approx(0.0)


def test_drawdown_computed_from_peak():
    store = MetricsStore()
    store.update_drawdown(1000.0)   # peak = 1000
    store.update_drawdown(900.0)    # drawdown = 100
    assert store.drawdown() == pytest.approx(100.0)


def test_drawdown_zero_at_new_high():
    store = MetricsStore()
    store.update_drawdown(1000.0)
    store.update_drawdown(900.0)
    store.update_drawdown(1100.0)   # new peak; drawdown resets to 0
    assert store.drawdown() == pytest.approx(0.0)


def test_drawdown_never_negative():
    store = MetricsStore()
    store.update_drawdown(1000.0)
    store.update_drawdown(1200.0)   # rising — drawdown = 0
    assert store.drawdown() >= 0.0


# ── Snapshot (FR-602) ─────────────────────────────────────────────────────────

def test_snapshot_contains_required_keys():
    store = MetricsStore()
    snap = store.snapshot()
    expected_keys = {
        "pnl_daily",
        "trades_total",
        "latency_p95_ms",
        "maker_ratio",
        "exposure_total",
        "drawdown",
    }
    assert expected_keys.issubset(snap.keys())


def test_snapshot_values_match_setters():
    store = MetricsStore()
    store.set_pnl_daily(5.0)
    store.inc_trades(10)
    store.observe_latency(30.0)
    store.set_maker_ratio(0.98)
    store.set_exposure(800.0)
    store.update_drawdown(1000.0)
    store.update_drawdown(950.0)

    snap = store.snapshot()
    assert snap["pnl_daily"] == pytest.approx(5.0)
    assert snap["trades_total"] == 10
    assert snap["maker_ratio"] == pytest.approx(0.98)
    assert snap["exposure_total"] == pytest.approx(800.0)
    assert snap["drawdown"] == pytest.approx(50.0)


# ── Metric types and Histogram buckets (FR-605) ───────────────────────────────

def test_six_metrics_exist():
    """All 6 FR-605 metrics must be declared in the module."""
    from metrics.prometheus import _LATENCY_BUCKETS, LatencyTracker, MetricsStore
    store = MetricsStore()
    # Verify all 6 attribute update methods exist and are callable
    for method in (
        "set_pnl_daily",      # bot_pnl_daily — Gauge
        "inc_trades",          # bot_trades_total — Counter
        "observe_latency",     # bot_latency_p95_ms — Histogram
        "set_maker_ratio",     # bot_maker_ratio — Gauge
        "set_exposure",        # bot_exposure_total — Gauge
        "update_drawdown",     # bot_drawdown — Gauge
    ):
        assert callable(getattr(store, method)), f"Missing method: {method}"


def test_histogram_has_exactly_7_buckets():
    """bot_latency_p95_ms Histogram must have exactly 7 buckets per FR-605."""
    from metrics.prometheus import _LATENCY_BUCKETS
    assert len(_LATENCY_BUCKETS) == 7


def test_histogram_bucket_values():
    """Bucket boundaries must be 10, 25, 50, 75, 100, 150, 200 ms."""
    from metrics.prometheus import _LATENCY_BUCKETS
    assert tuple(_LATENCY_BUCKETS) == (10, 25, 50, 75, 100, 150, 200)


def test_pnl_daily_is_gauge_semantics():
    """bot_pnl_daily resets to zero; a Counter would not support reset."""
    store = MetricsStore()
    store.set_pnl_daily(100.0)
    store.reset_pnl_daily()
    assert store.pnl_daily() == pytest.approx(0.0)


def test_trades_total_is_counter_semantics():
    """bot_trades_total is cumulative — it never decreases."""
    store = MetricsStore()
    store.inc_trades(5)
    store.inc_trades(3)
    assert store.trades_total() == 8


def test_maker_ratio_gauge_overwrites():
    """bot_maker_ratio is a Gauge — a new set overwrites the previous value."""
    store = MetricsStore()
    store.set_maker_ratio(0.80)
    store.set_maker_ratio(0.95)
    assert store.maker_ratio() == pytest.approx(0.95)


def test_exposure_gauge_overwrites():
    """bot_exposure_total is a Gauge — overwrites on each set."""
    store = MetricsStore()
    store.set_exposure(500.0)
    store.set_exposure(750.0)
    assert store.exposure_total() == pytest.approx(750.0)


def test_drawdown_gauge_reflects_latest():
    """bot_drawdown is a Gauge derived from peak equity."""
    store = MetricsStore()
    store.update_drawdown(1000.0)
    store.update_drawdown(800.0)
    assert store.drawdown() == pytest.approx(200.0)
    store.update_drawdown(1000.0)  # back to peak
    assert store.drawdown() == pytest.approx(0.0)
