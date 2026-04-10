"""
Unit tests for core/execution/reporting.py.

Covers: status_report_loop JSON emission (FR-602), daily_summary_loop
midnight crossing and pnl reset (FR-604), stale_quote_loop cancellation
and timestamp logic (FR-212).
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.execution.reporting import (
    daily_summary_loop,
    stale_quote_loop,
    status_report_loop,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_metrics(snap_override: dict | None = None) -> MagicMock:
    metrics = MagicMock()
    default_snap = {
        "exposure_total": 1000.0,
        "pnl_daily": 50.0,
        "drawdown": 10.0,
        "trades_total": 42,
        "latency_p95_ms": 75.0,
        "maker_ratio": 0.95,
        "active_tokens": ["tok1", "tok2"],
    }
    metrics.snapshot = MagicMock(return_value={**default_snap, **(snap_override or {})})
    metrics.reset_pnl_daily = MagicMock()
    return metrics


def make_reward_ledger() -> MagicMock:
    ledger = MagicMock()
    ledger.unscored_tokens = MagicMock(return_value=[])
    ledger.total_rewards_today = MagicMock(return_value=10.0)
    ledger.total_rebates_today = MagicMock(return_value=2.5)
    return ledger


def make_alerter() -> AsyncMock:
    alerter = AsyncMock()
    alerter.send_daily_summary = AsyncMock()
    return alerter


def make_order_executor() -> AsyncMock:
    executor = AsyncMock()
    executor.apply = AsyncMock()
    return executor


def pass_once_then_cancel():
    """Returns a fake_sleep that lets the first call pass and raises on the second."""
    call_count = [0]

    async def fake_sleep(_):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise asyncio.CancelledError

    return fake_sleep


# ── status_report_loop (FR-602) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_report_loop_emits_log():
    """Loop logs STATUS_REPORT after the first sleep."""
    metrics = make_metrics()

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.log") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await status_report_loop(metrics, {}, MagicMock(), make_alerter())

    metrics.snapshot.assert_called_once()
    assert mock_log.info.called
    assert "STATUS_REPORT" in mock_log.info.call_args[0][0]


@pytest.mark.asyncio
async def test_status_report_loop_sleep_interval():
    """Loop sleeps for 30 seconds."""
    sleep_delays = []

    async def fake_sleep(delay):
        sleep_delays.append(delay)
        raise asyncio.CancelledError

    with patch("core.execution.reporting.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await status_report_loop(make_metrics(), {}, MagicMock(), make_alerter())

    assert sleep_delays[0] == 30.0


@pytest.mark.asyncio
async def test_status_report_loop_report_fields():
    """The emitted report must contain all required FR-602 fields."""
    metrics = make_metrics()
    logged_messages: list = []

    def capture_info(fmt, *args, **_kw):
        logged_messages.append((fmt, args))

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.log") as mock_log:
            mock_log.info.side_effect = capture_info
            with pytest.raises(asyncio.CancelledError):
                await status_report_loop(metrics, {}, MagicMock(), make_alerter())

    assert logged_messages, "log.info was never called"
    fmt, args = logged_messages[0]
    assert "STATUS_REPORT" in fmt
    report = json.loads(args[0])

    required = {"event_type", "timestamp", "total_exposure", "daily_pnl",
                "drawdown", "trade_count", "p95_latency_ms", "maker_ratio"}
    assert required.issubset(report.keys())


@pytest.mark.asyncio
async def test_status_report_loop_error_does_not_stop_loop():
    """An exception in report generation must not stop the loop."""
    metrics = MagicMock()
    metrics.snapshot = MagicMock(side_effect=RuntimeError("oops"))

    call_count = [0]

    async def fake_sleep(_):
        call_count[0] += 1
        if call_count[0] >= 3:
            raise asyncio.CancelledError

    with patch("core.execution.reporting.asyncio.sleep", side_effect=fake_sleep):
        with patch("core.execution.reporting.log"):
            with pytest.raises(asyncio.CancelledError):
                await status_report_loop(metrics, {}, MagicMock(), make_alerter())

    assert call_count[0] == 3


# ── daily_summary_loop (FR-604) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_summary_loop_fires_at_midnight():
    """Loop emits summary when hour==0 and day is new."""
    from datetime import datetime, timezone
    fake_now = datetime(2025, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fake_now)
            with patch("core.execution.reporting.log"):
                with pytest.raises(asyncio.CancelledError):
                    await daily_summary_loop(
                        make_metrics(),
                        {"reward": make_reward_ledger()},
                        MagicMock(),
                        make_alerter(),
                    )

    # reset_pnl_daily is called on the metrics mock directly
    # (patching datetime doesn't affect the metrics mock)


@pytest.mark.asyncio
async def test_daily_summary_loop_resets_pnl_at_midnight():
    """FR-605: pnl gauge is reset at midnight."""
    from datetime import datetime, timezone
    fake_now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    metrics = make_metrics()

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fake_now)
            with patch("core.execution.reporting.log"):
                with pytest.raises(asyncio.CancelledError):
                    await daily_summary_loop(
                        metrics,
                        {"reward": make_reward_ledger()},
                        MagicMock(),
                        make_alerter(),
                    )

    metrics.reset_pnl_daily.assert_called_once()


@pytest.mark.asyncio
async def test_daily_summary_loop_does_not_fire_outside_midnight():
    """Loop does NOT emit summary when hour != 0."""
    from datetime import datetime, timezone
    fake_now = datetime(2025, 1, 2, 14, 0, 0, tzinfo=timezone.utc)
    metrics = make_metrics()
    alerter = make_alerter()

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fake_now)
            with pytest.raises(asyncio.CancelledError):
                await daily_summary_loop(metrics, {}, MagicMock(), alerter)

    metrics.reset_pnl_daily.assert_not_called()
    alerter.send_daily_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_summary_loop_does_not_fire_twice_same_day():
    """Second midnight tick on the same day must not reset again."""
    from datetime import datetime, timezone
    fake_now = datetime(2025, 1, 2, 0, 5, 0, tzinfo=timezone.utc)
    metrics = make_metrics()

    call_count = [0]

    async def fake_sleep(_):
        call_count[0] += 1
        if call_count[0] >= 3:
            raise asyncio.CancelledError

    with patch("core.execution.reporting.asyncio.sleep", side_effect=fake_sleep):
        with patch("core.execution.reporting.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fake_now)
            with patch("core.execution.reporting.log"):
                with pytest.raises(asyncio.CancelledError):
                    await daily_summary_loop(
                        metrics,
                        {"reward": make_reward_ledger()},
                        MagicMock(),
                        make_alerter(),
                    )

    assert metrics.reset_pnl_daily.call_count == 1


@pytest.mark.asyncio
async def test_daily_summary_loop_sends_alert():
    """Alerter.send_daily_summary is called at midnight."""
    from datetime import datetime, timezone
    fake_now = datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    alerter = make_alerter()

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fake_now)
            with patch("core.execution.reporting.log"):
                with pytest.raises(asyncio.CancelledError):
                    await daily_summary_loop(
                        make_metrics(),
                        {"reward": make_reward_ledger()},
                        MagicMock(),
                        alerter,
                    )

    alerter.send_daily_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_summary_check_interval():
    """Loop sleeps for 60 seconds per check."""
    sleep_delays = []

    async def fake_sleep(delay):
        sleep_delays.append(delay)
        raise asyncio.CancelledError

    with patch("core.execution.reporting.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await daily_summary_loop(make_metrics(), {}, MagicMock(), make_alerter())

    assert sleep_delays[0] == 60.0


# ── stale_quote_loop (FR-212) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_quote_loop_cancels_stale_order():
    """Orders not refreshed within timeout are cancelled."""
    executor = make_order_executor()
    now = time.time()
    stale_ts = now - 200

    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=now)
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop(
                    ["order_1"],
                    executor,
                    AsyncMock(),
                    settings,
                    {"order_1": stale_ts},
                )

    executor.apply.assert_awaited_once()
    mutations = executor.apply.call_args[0][0]
    assert len(mutations) == 1
    assert mutations[0].order_id == "order_1"
    assert mutations[0].reason == "stale"


@pytest.mark.asyncio
async def test_stale_quote_loop_does_not_cancel_fresh_order():
    """Orders refreshed recently are not cancelled."""
    executor = make_order_executor()
    now = time.time()
    fresh_ts = now - 5  # well within 60s timeout

    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=now)
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop(
                    ["order_fresh"],
                    executor,
                    AsyncMock(),
                    settings,
                    {"order_fresh": fresh_ts},
                )

    executor.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_quote_loop_no_orders():
    """Empty active orders list triggers no cancellations."""
    executor = make_order_executor()
    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=time.time())
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop([], executor, AsyncMock(), settings, {})

    executor.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_quote_loop_order_with_no_timestamp_is_stale():
    """An order_id with no recorded timestamp defaults to epoch 0 → always stale."""
    executor = make_order_executor()
    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=time.time())
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop(
                    ["unknown_order"],
                    executor,
                    AsyncMock(),
                    settings,
                    {},  # no timestamps recorded
                )

    executor.apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_quote_loop_accepts_callable_active_orders():
    """active_orders can be a callable returning a list."""
    executor = make_order_executor()
    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    now = time.time()
    stale_ts = now - 200

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=now)
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop(
                    lambda: ["callable_order"],
                    executor,
                    AsyncMock(),
                    settings,
                    {"callable_order": stale_ts},
                )

    executor.apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_quote_loop_cancels_multiple_stale_orders():
    """Multiple stale orders are all cancelled; fresh ones are skipped."""
    executor = make_order_executor()
    now = time.time()
    stale_ts = now - 200

    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    order_timestamps = {"o1": stale_ts, "o2": stale_ts, "o3": now - 5}

    with patch("core.execution.reporting.asyncio.sleep", side_effect=pass_once_then_cancel()):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=now)
            with pytest.raises(asyncio.CancelledError):
                await stale_quote_loop(
                    ["o1", "o2", "o3"],
                    executor,
                    AsyncMock(),
                    settings,
                    order_timestamps,
                )

    executor.apply.assert_awaited_once()
    mutations = executor.apply.call_args[0][0]
    stale_ids = {m.order_id for m in mutations}
    assert stale_ids == {"o1", "o2"}


@pytest.mark.asyncio
async def test_stale_quote_loop_error_does_not_stop_loop():
    """Exception during stale check is suppressed; loop continues."""
    executor = AsyncMock()
    executor.apply = AsyncMock(side_effect=RuntimeError("network error"))

    settings = MagicMock()
    settings.STALE_QUOTE_TIMEOUT_S = 60

    now = time.time()
    stale_ts = now - 200

    call_count = [0]

    async def fake_sleep(_):
        call_count[0] += 1
        if call_count[0] >= 3:
            raise asyncio.CancelledError

    with patch("core.execution.reporting.asyncio.sleep", side_effect=fake_sleep):
        with patch("core.execution.reporting.time") as mock_time:
            mock_time.time = MagicMock(return_value=now)
            with patch("core.execution.reporting.log"):
                with pytest.raises(asyncio.CancelledError):
                    await stale_quote_loop(
                        ["o1"],
                        executor,
                        AsyncMock(),
                        settings,
                        {"o1": stale_ts},
                    )

    assert call_count[0] == 3
