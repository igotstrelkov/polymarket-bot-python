"""
alerts/dispatcher.py — Alert event dispatcher.

Defines the canonical AlertEvent enum (FR-603) and a Dispatcher that maps
each event to a formatted message and routes it through the Alerter.

Design:
  - AlertEvent covers all observable system events that require notification.
  - Dispatcher.dispatch() is the single entry point for all alert emission.
  - Each event maps to a message template and an AlertLevel.
  - dispatch() accepts optional **kwargs used in message formatting.
  - All HTTP errors are handled inside Alerter; Dispatcher never propagates them.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Any

from alerts.alerter import AlertLevel, Alerter


class AlertEvent(Enum):
    KILL_SWITCH_ACTIVATED           = auto()
    DAILY_LOSS_LIMIT_HIT            = auto()
    WS_DISCONNECT_60S               = auto()
    INVENTORY_HALT_TRIGGERED        = auto()
    ZERO_TRADES_30MIN               = auto()
    MARKET_RESOLVED_WITH_POSITIONS  = auto()
    LATENCY_P95_EXCEEDED            = auto()
    RELAYER_FAILOVER_ACTIVATED      = auto()
    RELAYER_RECOVERED               = auto()
    FEE_CACHE_SUSTAINED_OUTAGE      = auto()
    REDEMPTION_FAILED_MANUAL_REQUIRED = auto()
    REDEMPTION_SUCCESS              = auto()
    SAFE_MODE_ENTERED               = auto()
    SAFE_MODE_EXITED                = auto()
    CANCEL_CONFIRM_MODE_ACTIVATED   = auto()


# Map event → (level, message_template)
# Templates use .format(**kwargs) where kwargs come from dispatch() call.
_EVENT_MAP: dict[AlertEvent, tuple[AlertLevel, str]] = {
    AlertEvent.KILL_SWITCH_ACTIVATED: (
        AlertLevel.CRITICAL,
        "Kill switch activated — all orders cancelled",
    ),
    AlertEvent.DAILY_LOSS_LIMIT_HIT: (
        AlertLevel.CRITICAL,
        "Daily loss limit hit: ${loss:.2f}",
    ),
    AlertEvent.WS_DISCONNECT_60S: (
        AlertLevel.WARNING,
        "WebSocket disconnected for {seconds:.0f}s",
    ),
    AlertEvent.INVENTORY_HALT_TRIGGERED: (
        AlertLevel.WARNING,
        "Inventory halt: token={token_id} skew={skew:.3f}",
    ),
    AlertEvent.ZERO_TRADES_30MIN: (
        AlertLevel.WARNING,
        "Zero trades for {minutes:.0f} minutes",
    ),
    AlertEvent.MARKET_RESOLVED_WITH_POSITIONS: (
        AlertLevel.WARNING,
        "Market resolved with open position: token={token_id} shares={shares:.2f}",
    ),
    AlertEvent.LATENCY_P95_EXCEEDED: (
        AlertLevel.WARNING,
        "P95 latency exceeded threshold: {p95_ms:.0f}ms",
    ),
    AlertEvent.RELAYER_FAILOVER_ACTIVATED: (
        AlertLevel.WARNING,
        "Relayer failover: EOA fallback activated",
    ),
    AlertEvent.RELAYER_RECOVERED: (
        AlertLevel.INFO,
        "Relayer recovered: Builder Relayer restored",
    ),
    AlertEvent.FEE_CACHE_SUSTAINED_OUTAGE: (
        AlertLevel.WARNING,
        "Fee cache sustained outage — fee rate unavailable",
    ),
    AlertEvent.REDEMPTION_FAILED_MANUAL_REQUIRED: (
        AlertLevel.CRITICAL,
        "Auto-redemption failed after {attempts} attempt(s): condition={condition_id} — manual action required",
    ),
    AlertEvent.REDEMPTION_SUCCESS: (
        AlertLevel.INFO,
        "Auto-redemption complete: condition={condition_id} USDC={usdc:.2f}",
    ),
    AlertEvent.SAFE_MODE_ENTERED: (
        AlertLevel.WARNING,
        "Safe mode entered: {reason}",
    ),
    AlertEvent.SAFE_MODE_EXITED: (
        AlertLevel.INFO,
        "Safe mode exited — normal operation resumed",
    ),
    AlertEvent.CANCEL_CONFIRM_MODE_ACTIVATED: (
        AlertLevel.WARNING,
        "Cancel-confirm mode activated at {threshold_pct:.1f}% failure rate",
    ),
}


class Dispatcher:
    """Routes AlertEvents through the Alerter to all configured channels.

    Usage:
        dispatcher = Dispatcher(alerter)
        await dispatcher.dispatch(AlertEvent.KILL_SWITCH_ACTIVATED)
        await dispatcher.dispatch(AlertEvent.DAILY_LOSS_LIMIT_HIT, loss=42.50)
    """

    def __init__(self, alerter: Alerter) -> None:
        self._alerter = alerter

    async def dispatch(self, event: AlertEvent, **kwargs: Any) -> None:
        """Dispatch *event* with optional formatting *kwargs*."""
        level, template = _EVENT_MAP[event]
        try:
            message = template.format(**kwargs)
        except (KeyError, ValueError):
            message = template  # send raw template if kwargs are missing
        await self._alerter.send(message, level=level)
