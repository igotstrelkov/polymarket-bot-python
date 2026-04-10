"""
Reporting loops — FR-602, FR-604, FR-212.

FR-602: Emit structured status report every 30 seconds.
FR-604: Emit daily summary at 00:00 UTC; reset bot_pnl_daily gauge.
FR-212: Stale-quote safety-net loop — cancel any quote not refreshed within
        STALE_QUOTE_TIMEOUT_S (backstop for WS gaps / executor failures).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_STATUS_REPORT_INTERVAL_S = 30.0
_DAILY_SUMMARY_CHECK_INTERVAL_S = 60.0   # check every minute for midnight crossing


async def status_report_loop(
    metrics: Any,      # MetricsStore
    ledgers: Any,      # dict with keys: fill, reward (optional)
    settings: Any,     # Settings
    alerter: Any,      # Alerter (optional — None disables alerts)
) -> None:
    """FR-602: emit structured JSON status report every 30 seconds.

    Fields: total_exposure, daily_pnl, drawdown, trade_count,
    active_subscriptions, inventory_warnings, p95_latency_ms.
    """
    while True:
        await asyncio.sleep(_STATUS_REPORT_INTERVAL_S)
        try:
            snap = metrics.snapshot()
            report = {
                "event_type": "status_report",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "total_exposure": snap.get("exposure_total", 0.0),
                "daily_pnl": snap.get("pnl_daily", 0.0),
                "drawdown": snap.get("drawdown", 0.0),
                "trade_count": snap.get("trades_total", 0),
                "p95_latency_ms": snap.get("latency_p95_ms", 0.0),
                "maker_ratio": snap.get("maker_ratio", 1.0),
            }
            log.info("STATUS_REPORT %s", json.dumps(report))
        except Exception:
            log.exception("status_report_loop: error generating report")


async def daily_summary_loop(
    metrics: Any,      # MetricsStore
    ledgers: Any,      # dict with fill/reward ledger references
    settings: Any,     # Settings
    alerter: Any,      # Alerter
) -> None:
    """FR-604: emit daily summary at 00:00 UTC and reset pnl_daily gauge.

    Summary contains: total_trades, net_pnl, exposure_by_strategy,
    estimated_liquidity_rewards, estimated_maker_rebates,
    maker_taker_ratio (A+C), avg_latency_ms, order_scoring_success_rate.
    """
    last_reset_day: int | None = None

    while True:
        await asyncio.sleep(_DAILY_SUMMARY_CHECK_INTERVAL_S)
        now = datetime.now(tz=timezone.utc)

        # Midnight crossing check: hour==0 and day changed
        if now.hour == 0 and now.day != last_reset_day:
            last_reset_day = now.day
            try:
                snap = metrics.snapshot()
                fill_ledger = ledgers.get("fill") if ledgers else None
                reward_ledger = ledgers.get("reward") if ledgers else None

                unscored_count = len(reward_ledger.unscored_tokens()) if reward_ledger else 0
                total_scored = len(snap.get("active_tokens", [])) if isinstance(snap, dict) else 0
                scoring_rate = (
                    1.0 - unscored_count / total_scored
                    if total_scored > 0 else 1.0
                )

                summary = {
                    "date": now.strftime("%Y-%m-%d"),
                    "total_trades": snap.get("trades_total", 0),
                    "net_pnl": snap.get("pnl_daily", 0.0),
                    "maker_ratio": snap.get("maker_ratio", 1.0),
                    "avg_latency_ms": snap.get("latency_p95_ms", 0.0),
                    "total_exposure": snap.get("exposure_total", 0.0),
                    "drawdown": snap.get("drawdown", 0.0),
                    "estimated_rewards": reward_ledger.total_rewards_today() if reward_ledger else 0.0,
                    "estimated_rebates": reward_ledger.total_rebates_today() if reward_ledger else 0.0,
                    "order_scoring_success_rate": round(scoring_rate, 4),
                }

                log.info("DAILY_SUMMARY %s", json.dumps(summary))
                if alerter:
                    await alerter.send_daily_summary(summary)

                # FR-605: reset pnl_daily gauge at midnight
                metrics.reset_pnl_daily()

            except Exception:
                log.exception("daily_summary_loop: error generating summary")


async def stale_quote_loop(
    active_orders: Any,      # Callable[[], list[order_id: str]] or dict-like
    order_executor: Any,     # has apply(mutations, clob_client)
    clob_client: Any,
    settings: Any,           # Settings with STALE_QUOTE_TIMEOUT_S
    order_timestamps: dict[str, float] | None = None,
) -> None:
    """FR-212: cancel quotes not refreshed within STALE_QUOTE_TIMEOUT_S.

    This is a backstop — in normal operation event-driven cancel/replace keeps
    quotes current. This loop catches any that fall through WS gaps.
    """
    if order_timestamps is None:
        order_timestamps = {}

    timeout_s = getattr(settings, "STALE_QUOTE_TIMEOUT_S", 60)

    while True:
        await asyncio.sleep(timeout_s)
        try:
            now = time.time()
            stale_ids: list[str] = []

            # Determine which orders are stale
            orders = active_orders() if callable(active_orders) else list(active_orders)
            for order_id in orders:
                last_refresh = order_timestamps.get(order_id, 0.0)
                if now - last_refresh > timeout_s:
                    stale_ids.append(order_id)

            if stale_ids:
                log.warning(
                    "stale_quote_loop: cancelling %d stale orders: %s",
                    len(stale_ids), stale_ids[:5],
                )
                from core.execution.execution_actor import CancelMutation
                mutations = [
                    CancelMutation(order_id=oid, token_id="", reason="stale")
                    for oid in stale_ids
                ]
                await order_executor.apply(mutations, clob_client)

        except Exception:
            log.exception("stale_quote_loop: error during stale check")
