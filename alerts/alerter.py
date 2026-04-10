"""
Alerter — Telegram and Discord webhook notifications.

FR-603: Send alerts for: kill switch activation, daily loss limit hit,
        WebSocket disconnect > 60s, inventory halt, zero trades for 30+ min,
        market resolution with held positions, P95 latency exceeding threshold
        for 60s, Relayer failover, fee cache sustained outage.
FR-604: Emit daily summary at 00:00 UTC.

Design:
  - Both webhooks are optional (empty string = disabled).
  - send() is fire-and-forget async; callers do not await results.
  - All messages include a UTC timestamp prefix.
  - HTTP errors are logged but never propagated (alerting must not block trading).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)

_MAX_MSG_LEN = 4000   # Telegram limit is ~4096; Discord 2000 for regular, 4096 for embeds


class AlertLevel(Enum):
    INFO    = auto()
    WARNING = auto()
    CRITICAL = auto()


class Alerter:
    """Sends alerts to Telegram and/or Discord webhooks.

    Usage:
        alerter = Alerter(http_client=..., telegram_url=..., discord_url=...)
        await alerter.send("Kill switch activated", level=AlertLevel.CRITICAL)
        await alerter.send_daily_summary(summary_dict)
    """

    def __init__(
        self,
        http_client: Any,           # httpx.AsyncClient or similar
        telegram_url: str = "",
        discord_url: str = "",
    ) -> None:
        self._http = http_client
        self._telegram_url = telegram_url
        self._discord_url = discord_url

    # ── Public API ────────────────────────────────────────────────────────────

    async def send(self, message: str, level: AlertLevel = AlertLevel.INFO) -> None:
        """Send *message* to all configured webhooks."""
        prefix = self._level_prefix(level)
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        full = f"[{ts}] {prefix} {message}"[:_MAX_MSG_LEN]

        if self._telegram_url:
            await self._send_telegram(full)
        if self._discord_url:
            await self._send_discord(full, level)

    async def send_daily_summary(self, summary: dict[str, Any]) -> None:
        """FR-604: format and send the daily operational summary."""
        lines = ["=== DAILY SUMMARY ==="]
        for key, value in summary.items():
            lines.append(f"  {key}: {value}")
        await self.send("\n".join(lines), level=AlertLevel.INFO)

    # ── Convenience wrappers (FR-603) ─────────────────────────────────────────

    async def kill_switch(self) -> None:
        await self.send("Kill switch activated — all orders cancelled", AlertLevel.CRITICAL)

    async def daily_loss_limit(self, loss: float) -> None:
        await self.send(f"Daily loss limit hit: ${loss:.2f}", AlertLevel.CRITICAL)

    async def ws_disconnect(self, seconds: float) -> None:
        await self.send(f"WebSocket disconnected for {seconds:.0f}s", AlertLevel.WARNING)

    async def inventory_halt(self, token_id: str, skew: float) -> None:
        await self.send(f"Inventory halt: {token_id} skew={skew:.3f}", AlertLevel.WARNING)

    async def zero_trades(self, minutes: float) -> None:
        await self.send(f"Zero trades for {minutes:.0f} minutes", AlertLevel.WARNING)

    async def resolution_with_position(self, token_id: str, shares: float) -> None:
        await self.send(
            f"Market resolved with open position: {token_id} shares={shares:.2f}",
            AlertLevel.WARNING,
        )

    async def latency_alert(self, p95_ms: float) -> None:
        await self.send(f"P95 latency exceeded threshold: {p95_ms:.0f}ms", AlertLevel.WARNING)

    async def relayer_failover(self, to_eoa: bool) -> None:
        direction = "EOA fallback activated" if to_eoa else "Relayer recovered"
        await self.send(f"Relayer failover: {direction}", AlertLevel.WARNING)

    async def fee_cache_outage(self) -> None:
        await self.send("Fee cache sustained outage", AlertLevel.WARNING)

    async def redemption_success(self, condition_id: str, usdc: float) -> None:
        await self.send(
            f"Auto-redemption complete: condition={condition_id} USDC={usdc:.2f}",
            AlertLevel.INFO,
        )

    async def redemption_failed(self, condition_id: str, attempts: int) -> None:
        await self.send(
            f"Auto-redemption failed after {attempts} attempts: {condition_id} — manual action required",
            AlertLevel.CRITICAL,
        )

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    async def _send_telegram(self, text: str) -> None:
        try:
            resp = await self._http.post(
                self._telegram_url,
                json={"text": text, "parse_mode": "HTML"},
            )
            resp.raise_for_status()
        except Exception:
            log.exception("Alerter: failed to send Telegram message")

    async def _send_discord(self, text: str, level: AlertLevel) -> None:
        color = {
            AlertLevel.INFO: 0x2ECC71,
            AlertLevel.WARNING: 0xF39C12,
            AlertLevel.CRITICAL: 0xE74C3C,
        }.get(level, 0xFFFFFF)
        payload: dict[str, Any] = {
            "embeds": [{
                "description": text,
                "color": color,
            }]
        }
        try:
            resp = await self._http.post(self._discord_url, json=payload)
            resp.raise_for_status()
        except Exception:
            log.exception("Alerter: failed to send Discord message")

    @staticmethod
    def _level_prefix(level: AlertLevel) -> str:
        return {
            AlertLevel.INFO: "[INFO]",
            AlertLevel.WARNING: "[WARN]",
            AlertLevel.CRITICAL: "[CRIT]",
        }.get(level, "")
