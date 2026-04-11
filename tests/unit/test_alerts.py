"""
Unit tests for alerts/dispatcher.py and alerts/alerter.py.

Covers:
- Every AlertEvent dispatches a non-empty message to at least one channel.
- AlertLevel routing: CRITICAL → Telegram + Discord; INFO → both when configured.
- HTTP errors are suppressed (never propagate to caller).
- Telegram/Discord payloads have correct structure.
- Daily summary formatting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alerts.alerter import AlertLevel, Alerter
from alerts.dispatcher import AlertEvent, Dispatcher


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_http_client() -> AsyncMock:
    client = AsyncMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


def make_alerter(telegram_url: str = "http://tg", discord_url: str = "http://dc") -> Alerter:
    return Alerter(
        http_client=make_http_client(),
        telegram_url=telegram_url,
        discord_url=discord_url,
    )


def make_dispatcher(**kwargs) -> tuple[Dispatcher, Alerter]:
    alerter = make_alerter(**kwargs)
    return Dispatcher(alerter), alerter


# ── kwargs required per event ─────────────────────────────────────────────────

_EVENT_KWARGS: dict[AlertEvent, dict] = {
    AlertEvent.KILL_SWITCH_ACTIVATED: {},
    AlertEvent.DAILY_LOSS_LIMIT_HIT: {"loss": 100.0},
    AlertEvent.WS_DISCONNECT_60S: {"seconds": 75.0},
    AlertEvent.INVENTORY_HALT_TRIGGERED: {"token_id": "tok_1", "skew": 0.45},
    AlertEvent.ZERO_TRADES_30MIN: {"minutes": 35.0},
    AlertEvent.MARKET_RESOLVED_WITH_POSITIONS: {"token_id": "tok_2", "shares": 50.0},
    AlertEvent.LATENCY_P95_EXCEEDED: {"p95_ms": 250.0},
    AlertEvent.RELAYER_FAILOVER_ACTIVATED: {},
    AlertEvent.RELAYER_RECOVERED: {},
    AlertEvent.FEE_CACHE_SUSTAINED_OUTAGE: {},
    AlertEvent.REDEMPTION_FAILED_MANUAL_REQUIRED: {"condition_id": "cond_1", "attempts": 3},
    AlertEvent.REDEMPTION_SUCCESS: {"condition_id": "cond_2", "usdc": 50.0},
    AlertEvent.SAFE_MODE_ENTERED: {"reason": "high resource usage"},
    AlertEvent.SAFE_MODE_EXITED: {},
    AlertEvent.CANCEL_CONFIRM_MODE_ACTIVATED: {"threshold_pct": 15.0},
}


# ── Every AlertEvent dispatches a non-empty message ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("event", list(AlertEvent))
async def test_every_alert_event_sends_to_at_least_one_channel(event):
    """Each AlertEvent must produce a non-empty dispatch to at least one channel."""
    dispatcher, alerter = make_dispatcher()
    kwargs = _EVENT_KWARGS[event]

    await dispatcher.dispatch(event, **kwargs)

    assert alerter._http.post.await_count >= 1

    sent_texts = []
    for call in alerter._http.post.await_args_list:
        payload = call.kwargs.get("json") or {}
        if "text" in payload:
            sent_texts.append(payload["text"])
        elif "embeds" in payload:
            for embed in payload["embeds"]:
                sent_texts.append(embed.get("description", ""))

    assert any(t.strip() for t in sent_texts), f"Empty dispatch for {event}"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", list(AlertEvent))
async def test_every_alert_event_enum_is_mapped(event):
    """Every AlertEvent has an entry in _EVENT_MAP (no KeyError on dispatch)."""
    from alerts.dispatcher import _EVENT_MAP
    assert event in _EVENT_MAP


def test_all_15_alert_events_defined():
    """AlertEvent must have exactly the 15 events specified in the plan."""
    expected = {
        "KILL_SWITCH_ACTIVATED",
        "DAILY_LOSS_LIMIT_HIT",
        "WS_DISCONNECT_60S",
        "INVENTORY_HALT_TRIGGERED",
        "ZERO_TRADES_30MIN",
        "MARKET_RESOLVED_WITH_POSITIONS",
        "LATENCY_P95_EXCEEDED",
        "RELAYER_FAILOVER_ACTIVATED",
        "RELAYER_RECOVERED",
        "FEE_CACHE_SUSTAINED_OUTAGE",
        "REDEMPTION_FAILED_MANUAL_REQUIRED",
        "REDEMPTION_SUCCESS",
        "SAFE_MODE_ENTERED",
        "SAFE_MODE_EXITED",
        "CANCEL_CONFIRM_MODE_ACTIVATED",
    }
    actual = {e.name for e in AlertEvent}
    assert actual == expected


# ── AlertLevel routing ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_calls_telegram_when_configured():
    alerter = make_alerter()
    await alerter.send("test message", AlertLevel.INFO)
    urls_called = [c.args[0] for c in alerter._http.post.await_args_list]
    assert "http://tg" in urls_called


@pytest.mark.asyncio
async def test_send_calls_discord_when_configured():
    alerter = make_alerter()
    await alerter.send("test message", AlertLevel.INFO)
    urls_called = [c.args[0] for c in alerter._http.post.await_args_list]
    assert "http://dc" in urls_called


@pytest.mark.asyncio
async def test_send_skips_telegram_when_url_empty():
    alerter = make_alerter(telegram_url="")
    await alerter.send("msg", AlertLevel.INFO)
    urls_called = [c.args[0] for c in alerter._http.post.await_args_list]
    assert not any("tg" in u for u in urls_called)


@pytest.mark.asyncio
async def test_send_skips_discord_when_url_empty():
    alerter = make_alerter(discord_url="")
    await alerter.send("msg", AlertLevel.INFO)
    urls_called = [c.args[0] for c in alerter._http.post.await_args_list]
    assert not any("dc" in u for u in urls_called)


@pytest.mark.asyncio
async def test_send_no_webhooks_does_not_raise():
    alerter = make_alerter(telegram_url="", discord_url="")
    await alerter.send("msg", AlertLevel.CRITICAL)


# ── Message prefix correctness ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_info_level_includes_info_prefix():
    alerter = make_alerter(discord_url="")
    await alerter.send("hello", AlertLevel.INFO)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "[INFO]" in payload["text"]


@pytest.mark.asyncio
async def test_critical_level_includes_crit_prefix():
    alerter = make_alerter(discord_url="")
    await alerter.send("hello", AlertLevel.CRITICAL)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "[CRIT]" in payload["text"]


# ── HTTP error suppression ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_http_error_does_not_propagate():
    alerter = Alerter(
        http_client=AsyncMock(post=AsyncMock(side_effect=Exception("conn refused"))),
        telegram_url="http://tg",
        discord_url="",
    )
    await alerter.send("msg", AlertLevel.CRITICAL)


@pytest.mark.asyncio
async def test_discord_http_error_does_not_propagate():
    alerter = Alerter(
        http_client=AsyncMock(post=AsyncMock(side_effect=Exception("conn refused"))),
        telegram_url="",
        discord_url="http://dc",
    )
    await alerter.send("msg", AlertLevel.CRITICAL)


# ── Discord payload structure ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_payload_has_embeds():
    alerter = make_alerter(telegram_url="")
    await alerter.send("embed test", AlertLevel.WARNING)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "embeds" in payload
    assert payload["embeds"][0]["description"]


@pytest.mark.asyncio
async def test_discord_embed_color_critical():
    alerter = make_alerter(telegram_url="")
    await alerter.send("critical test", AlertLevel.CRITICAL)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert payload["embeds"][0]["color"] == 0xE74C3C


@pytest.mark.asyncio
async def test_discord_embed_color_warning():
    alerter = make_alerter(telegram_url="")
    await alerter.send("warn test", AlertLevel.WARNING)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert payload["embeds"][0]["color"] == 0xF39C12


@pytest.mark.asyncio
async def test_telegram_payload_has_parse_mode():
    alerter = make_alerter(discord_url="")
    await alerter.send("tg test", AlertLevel.INFO)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert payload.get("parse_mode") == "HTML"


# ── Daily summary ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_daily_summary_includes_keys():
    alerter = make_alerter(discord_url="")
    await alerter.send_daily_summary({"net_pnl": 10.0, "total_trades": 42})
    payload = alerter._http.post.call_args.kwargs["json"]
    text = payload["text"]
    assert "net_pnl" in text
    assert "total_trades" in text


# ── Dispatcher message content ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatcher_kill_switch_sends():
    dispatcher, alerter = make_dispatcher()
    await dispatcher.dispatch(AlertEvent.KILL_SWITCH_ACTIVATED)
    alerter._http.post.assert_awaited()


@pytest.mark.asyncio
async def test_dispatcher_daily_loss_message_contains_loss():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.DAILY_LOSS_LIMIT_HIT, loss=75.25)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "75.25" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_latency_message_contains_value():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.LATENCY_P95_EXCEEDED, p95_ms=300.0)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "300" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_inventory_halt_message_contains_token():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.INVENTORY_HALT_TRIGGERED, token_id="tok_99", skew=0.55)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "tok_99" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_safe_mode_message_contains_reason():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.SAFE_MODE_ENTERED, reason="resource limit")
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "resource limit" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_redemption_failed_contains_condition():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(
        AlertEvent.REDEMPTION_FAILED_MANUAL_REQUIRED, condition_id="cond_x", attempts=3
    )
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "cond_x" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_cancel_confirm_contains_threshold():
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.CANCEL_CONFIRM_MODE_ACTIVATED, threshold_pct=20.0)
    payload = alerter._http.post.call_args.kwargs["json"]
    assert "20.0" in payload["text"]


@pytest.mark.asyncio
async def test_dispatcher_missing_kwargs_does_not_raise():
    """If kwargs are missing, the raw template is sent rather than crashing."""
    dispatcher, alerter = make_dispatcher(discord_url="")
    await dispatcher.dispatch(AlertEvent.DAILY_LOSS_LIMIT_HIT)  # 'loss' omitted
    alerter._http.post.assert_awaited()
