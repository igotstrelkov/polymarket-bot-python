"""
Unit tests for alerts/alerter.py.

Covers: Telegram/Discord dispatch, level prefix formatting, disabled webhook
skipping, HTTP error suppression, daily summary formatting, and convenience
wrappers (FR-603).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alerts.alerter import AlertLevel, Alerter


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_alerter(
    telegram_url: str = "https://telegram.example.com",
    discord_url: str = "https://discord.example.com",
) -> tuple[Alerter, AsyncMock]:
    http = AsyncMock()
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    http.post = AsyncMock(return_value=resp)
    alerter = Alerter(http_client=http, telegram_url=telegram_url, discord_url=discord_url)
    return alerter, http


# ── Dispatch to webhooks ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_calls_telegram_when_configured():
    alerter, http = make_alerter()
    await alerter.send("test message")
    calls = [str(call) for call in http.post.call_args_list]
    assert any("telegram" in c for c in calls)


@pytest.mark.asyncio
async def test_send_calls_discord_when_configured():
    alerter, http = make_alerter()
    await alerter.send("test message")
    calls = [str(call) for call in http.post.call_args_list]
    assert any("discord" in c for c in calls)


@pytest.mark.asyncio
async def test_send_skips_telegram_when_url_empty():
    alerter, http = make_alerter(telegram_url="")
    await alerter.send("msg")
    calls = [str(call) for call in http.post.call_args_list]
    assert not any("telegram" in c for c in calls)


@pytest.mark.asyncio
async def test_send_skips_discord_when_url_empty():
    alerter, http = make_alerter(discord_url="")
    await alerter.send("msg")
    calls = [str(call) for call in http.post.call_args_list]
    assert not any("discord" in c for c in calls)


@pytest.mark.asyncio
async def test_send_no_webhooks_does_not_raise():
    alerter, _ = make_alerter(telegram_url="", discord_url="")
    await alerter.send("silent message")  # must not raise


# ── Level prefix ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_info_level_includes_info_prefix():
    alerter, http = make_alerter(discord_url="")
    await alerter.send("something", level=AlertLevel.INFO)
    payload = http.post.call_args[1]["json"]
    assert "[INFO]" in payload["text"]


@pytest.mark.asyncio
async def test_critical_level_includes_crit_prefix():
    alerter, http = make_alerter(discord_url="")
    await alerter.send("something", level=AlertLevel.CRITICAL)
    payload = http.post.call_args[1]["json"]
    assert "[CRIT]" in payload["text"]


# ── HTTP error suppression ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_http_error_does_not_propagate():
    http = AsyncMock()
    resp = AsyncMock()
    resp.raise_for_status = MagicMock(side_effect=Exception("503"))
    http.post = AsyncMock(return_value=resp)
    alerter = Alerter(http_client=http, telegram_url="https://t.example.com", discord_url="")
    await alerter.send("msg")   # must not raise


@pytest.mark.asyncio
async def test_discord_http_error_does_not_propagate():
    http = AsyncMock()
    http.post = AsyncMock(side_effect=Exception("network error"))
    alerter = Alerter(http_client=http, telegram_url="", discord_url="https://d.example.com")
    await alerter.send("msg")   # must not raise


# ── Daily summary (FR-604) ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_daily_summary_includes_keys():
    alerter, http = make_alerter(discord_url="")
    summary = {"total_trades": 42, "net_pnl": 12.50}
    await alerter.send_daily_summary(summary)
    payload = http.post.call_args[1]["json"]
    assert "total_trades" in payload["text"]
    assert "net_pnl" in payload["text"]


# ── Convenience wrappers (FR-603) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_switch_alert_sends():
    alerter, http = make_alerter(discord_url="")
    await alerter.kill_switch()
    payload = http.post.call_args[1]["json"]
    assert "kill" in payload["text"].lower() or "Kill" in payload["text"]


@pytest.mark.asyncio
async def test_inventory_halt_alert_includes_token():
    alerter, http = make_alerter(discord_url="")
    await alerter.inventory_halt("tok_xyz", skew=0.85)
    payload = http.post.call_args[1]["json"]
    assert "tok_xyz" in payload["text"]


@pytest.mark.asyncio
async def test_latency_alert_includes_value():
    alerter, http = make_alerter(discord_url="")
    await alerter.latency_alert(p95_ms=175.0)
    payload = http.post.call_args[1]["json"]
    assert "175" in payload["text"]
