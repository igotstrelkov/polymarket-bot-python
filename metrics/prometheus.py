"""
Prometheus metrics export.

FR-605: Export metrics on PROMETHEUS_PORT:
  bot_pnl_daily        Gauge    — resets at midnight
  bot_trades_total     Counter  — lifetime trade count
  bot_latency_p95_ms   Histogram — buckets: 10, 25, 50, 75, 100, 150, 200ms
  bot_maker_ratio      Gauge    — rolling 1-hour, A+C only
  bot_exposure_total   Gauge    — USD
  bot_drawdown         Gauge    — USD from peak

Design:
  - Uses the prometheus_client library (already in pyproject.toml dependencies).
  - MetricsStore is a thin façade that wraps prometheus_client objects.
  - All updates are synchronous (prometheus_client is thread-safe).
  - LatencyTracker maintains a fixed-window list for P95 computation and
    feeds it into the Histogram on each observe() call.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Histogram latency buckets (ms) per FR-605
_LATENCY_BUCKETS = (10, 25, 50, 75, 100, 150, 200)


class LatencyTracker:
    """Rolling window of latency samples for P95 calculation.

    Stores the last *maxlen* values (default 1000) and computes the P95
    on demand. Thread-safe through GIL only — adequate for a single asyncio loop.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def p95(self) -> float:
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = math.ceil(0.95 * len(sorted_samples)) - 1
        return sorted_samples[max(0, idx)]

    def count(self) -> int:
        return len(self._samples)


class MetricsStore:
    """Wraps prometheus_client metrics.  Falls back gracefully if the library
    is unavailable (e.g. in unit tests that don't install it).

    Usage:
        store = MetricsStore()
        store.set_pnl_daily(12.50)
        store.inc_trades()
        store.observe_latency(42.0)
        store.set_maker_ratio(0.97)
        store.set_exposure(800.0)
        store.set_drawdown(25.0)
    """

    def __init__(self) -> None:
        self._latency_tracker = LatencyTracker()
        self._pnl_daily: float = 0.0
        self._trades_total: int = 0
        self._maker_ratio: float = 1.0
        self._exposure_total: float = 0.0
        self._drawdown: float = 0.0
        self._peak_equity: float = 0.0

        self._prometheus_available = False
        try:
            from prometheus_client import Counter, Gauge, Histogram
            self._gauge_pnl = Gauge("bot_pnl_daily", "Daily P&L in USDC")
            self._counter_trades = Counter("bot_trades_total", "Lifetime trade count")
            self._hist_latency = Histogram(
                "bot_latency_p95_ms",
                "Cancel/replace pipeline latency",
                buckets=_LATENCY_BUCKETS,
            )
            self._gauge_maker = Gauge("bot_maker_ratio", "Rolling 1-hour maker ratio (A+C)")
            self._gauge_exposure = Gauge("bot_exposure_total", "Total USD exposure")
            self._gauge_drawdown = Gauge("bot_drawdown", "Drawdown from equity peak (USD)")
            self._prometheus_available = True
            log.info("MetricsStore: prometheus_client initialized")
        except Exception:
            log.info("MetricsStore: prometheus_client not available; in-memory only")

    # ── Update methods ────────────────────────────────────────────────────────

    def set_pnl_daily(self, pnl: float) -> None:
        self._pnl_daily = pnl
        if self._prometheus_available:
            self._gauge_pnl.set(pnl)

    def reset_pnl_daily(self) -> None:
        """Call at UTC midnight."""
        self.set_pnl_daily(0.0)

    def inc_trades(self, count: int = 1) -> None:
        self._trades_total += count
        if self._prometheus_available:
            self._counter_trades.inc(count)

    def observe_latency(self, latency_ms: float) -> None:
        self._latency_tracker.record(latency_ms)
        if self._prometheus_available:
            self._hist_latency.observe(latency_ms)

    def set_maker_ratio(self, ratio: float) -> None:
        self._maker_ratio = ratio
        if self._prometheus_available:
            self._gauge_maker.set(ratio)

    def set_exposure(self, exposure_usd: float) -> None:
        self._exposure_total = exposure_usd
        if self._prometheus_available:
            self._gauge_exposure.set(exposure_usd)

    def update_drawdown(self, current_equity: float) -> None:
        """Update peak equity and recompute drawdown."""
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
        drawdown = max(0.0, self._peak_equity - current_equity)
        self._drawdown = drawdown
        if self._prometheus_available:
            self._gauge_drawdown.set(drawdown)

    # ── Read methods ──────────────────────────────────────────────────────────

    def pnl_daily(self) -> float:
        return self._pnl_daily

    def trades_total(self) -> int:
        return self._trades_total

    def latency_p95(self) -> float:
        return self._latency_tracker.p95()

    def maker_ratio(self) -> float:
        return self._maker_ratio

    def exposure_total(self) -> float:
        return self._exposure_total

    def drawdown(self) -> float:
        return self._drawdown

    def snapshot(self) -> dict[str, float | int]:
        """FR-602: snapshot for 30-second status report."""
        return {
            "pnl_daily": self._pnl_daily,
            "trades_total": self._trades_total,
            "latency_p95_ms": self._latency_tracker.p95(),
            "maker_ratio": self._maker_ratio,
            "exposure_total": self._exposure_total,
            "drawdown": self._drawdown,
        }
