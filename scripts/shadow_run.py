"""
Shadow run — §11.4 Step 1 (24-hour latency gate).

Usage:
    DRY_RUN=false MAX_TOTAL_EXPOSURE=0 python scripts/shadow_run.py

Places real orders via the live API with MAX_TOTAL_EXPOSURE=0 so the Risk Gate
cancels every order immediately after dispatch. Measures P95 cancel/replace
latency over a configurable run window (default: 24 hours).

Exit 0 if P95 < 100ms.
Exit 1 if P95 ≥ 100ms or if any setup step fails.

NOTE: DRY_RUN=true cannot substitute — it skips all API calls.
NOTE: Requires funded wallet, live credentials, and MATIC for gas.
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

_DEFAULT_RUN_DURATION_S = 86_400   # 24 hours
_P95_THRESHOLD_MS = 100.0
_REPORT_INTERVAL_S = 300           # status log every 5 minutes
_MIN_SAMPLES_FOR_P95 = 10          # require at least this many samples


def _percentile(values: list[float], pct: float) -> float:
    """Compute the pct-th percentile of a sorted list."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (k - lo)


class LatencyCollector:
    """Thread-safe (asyncio) latency sample store."""

    def __init__(self) -> None:
        self._samples: list[float] = []   # milliseconds

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def p95(self) -> float:
        return _percentile(self._samples, 95.0)

    def count(self) -> int:
        return len(self._samples)

    def mean(self) -> float:
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)


async def _run() -> None:
    from config.settings import Settings
    from auth.credentials import CLOB_HOST, CHAIN_ID, derive_credentials, build_clob_client
    import httpx

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    if s.DRY_RUN:
        log.error(
            "DRY_RUN=true — shadow_run.py requires live API access. "
            "Set DRY_RUN=false to proceed."
        )
        sys.exit(1)

    if s.MAX_TOTAL_EXPOSURE != 0:
        log.warning(
            "MAX_TOTAL_EXPOSURE=%s — shadow run should be run with MAX_TOTAL_EXPOSURE=0 "
            "to ensure Risk Gate cancels every order immediately.",
            s.MAX_TOTAL_EXPOSURE,
        )

    run_duration_s = _DEFAULT_RUN_DURATION_S
    log.info("=== Shadow Run — %dh latency gate ===", run_duration_s // 3600)
    log.info("P95 threshold: %.0fms | Minimum samples: %d", _P95_THRESHOLD_MS, _MIN_SAMPLES_FOR_P95)

    creds = await derive_credentials(s.PRIVATE_KEY, CLOB_HOST, CHAIN_ID)
    clob = build_clob_client(s, creds)
    collector = LatencyCollector()

    # Shutdown flag set by SIGINT/SIGTERM
    _stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)

    deadline = time.monotonic() + run_duration_s
    last_report_ts = time.monotonic()

    async with httpx.AsyncClient(timeout=5.0) as http:
        while not _stop.is_set() and time.monotonic() < deadline:
            # Discover a market to probe
            try:
                r = await http.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"active": "true", "closed": "false", "limit": "5",
                            "order": "volumeNum", "ascending": "true"},
                )
                r.raise_for_status()
                markets = r.json()
            except Exception as exc:
                log.warning("Market discovery failed: %s — retrying in 10s", exc)
                await asyncio.sleep(10)
                continue

            for m in markets:
                if _stop.is_set():
                    break
                tokens = m.get("tokens") or m.get("clobTokenIds") or []
                if not tokens:
                    continue
                if not m.get("acceptingOrders", True):
                    continue
                token_id = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
                if not token_id:
                    continue

                # Measure place-then-cancel latency
                try:
                    from py_clob_client.clob_types import OrderArgs  # type: ignore[import]

                    order_args = OrderArgs(
                        token_id=token_id,
                        price=0.50,
                        size=0.01,
                        side="BUY",
                    )
                    t0 = time.monotonic()
                    resp = clob.create_and_post_order(order_args)
                    order_id = None
                    if isinstance(resp, dict):
                        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
                    if order_id:
                        clob.cancel(order_id)
                    latency_ms = (time.monotonic() - t0) * 1000
                    collector.record(latency_ms)
                    log.debug("Place+cancel: %.1fms (n=%d)", latency_ms, collector.count())
                except Exception as exc:
                    log.debug("Place/cancel failed: %s", exc)

            # Periodic status report
            now = time.monotonic()
            if now - last_report_ts >= _REPORT_INTERVAL_S:
                n = collector.count()
                p95 = collector.p95() if n >= _MIN_SAMPLES_FOR_P95 else float("nan")
                elapsed_h = (now - (deadline - run_duration_s)) / 3600
                remaining_h = (deadline - now) / 3600
                log.info(
                    "Status: n=%d mean=%.1fms p95=%.1fms "
                    "elapsed=%.1fh remaining=%.1fh",
                    n, collector.mean(), p95, elapsed_h, remaining_h,
                )
                last_report_ts = now

            await asyncio.sleep(1.0)

    # ── Final report ───────────────────────────────────────────────────────────
    n = collector.count()
    log.info("=== Shadow Run Complete ===")
    log.info("Samples collected: %d", n)

    if n < _MIN_SAMPLES_FOR_P95:
        log.error(
            "FAIL: insufficient samples (%d < %d) to compute P95",
            n, _MIN_SAMPLES_FOR_P95,
        )
        sys.exit(1)

    p95 = collector.p95()
    log.info("P95 cancel/replace latency: %.1fms (threshold: %.0fms)", p95, _P95_THRESHOLD_MS)
    log.info("Mean latency: %.1fms", collector.mean())

    if p95 < _P95_THRESHOLD_MS:
        log.info("PASS: P95 %.1fms < %.0fms threshold", p95, _P95_THRESHOLD_MS)
        sys.exit(0)
    else:
        log.error("FAIL: P95 %.1fms ≥ %.0fms threshold", p95, _P95_THRESHOLD_MS)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_run())
