"""
User WebSocket gateway.

FR-107: Authenticated user channel for fills, cancels, and order acks.
FR-108: Exponential backoff reconnect.

Auth note (PRD §4.10): credentials are sent in the subscription message body
AFTER the connection is established — NOT as HTTP headers on the handshake.
On reconnect, re-send auth then re-subscribe to all current condition IDs.

Subscription uses condition IDs (not token/asset IDs).
"""

import asyncio
import json
import logging
import time

import websockets

from auth.credentials import ApiCreds
from core.execution.types import CancelEvent, FillEvent, OrderAckEvent

log = logging.getLogger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0


class UserStreamGateway:
    """Streams authenticated user events (fills, cancels, acks).

    Emits typed events to separate queues per event type.
    Auth is sent as the first message on the socket after connect.
    """

    def __init__(
        self,
        creds: ApiCreds,
        fill_queue: asyncio.Queue,
        cancel_queue: asyncio.Queue,
        ack_queue: asyncio.Queue,
    ) -> None:
        self._creds = creds
        self._fill_queue = fill_queue
        self._cancel_queue = cancel_queue
        self._ack_queue = ack_queue
        self._condition_ids: set[str] = set()
        self._ws = None
        self._running = False

    async def connect(self) -> None:
        """Connect, authenticate, subscribe, and stream. Reconnects with backoff."""
        self._running = True
        backoff = _BACKOFF_INITIAL
        while self._running:
            try:
                async with websockets.connect(USER_WS_URL) as ws:
                    self._ws = ws
                    backoff = _BACKOFF_INITIAL
                    log.info("User WS connected")

                    # Auth is in the subscription message body, not HTTP headers
                    await ws.send(json.dumps({
                        "auth": {
                            "apiKey": self._creds.api_key,
                            "secret": self._creds.secret,
                            "passphrase": self._creds.passphrase,
                        },
                        "type": "user",
                    }))

                    if self._condition_ids:
                        await self._send_subscribe(ws, list(self._condition_ids))

                    async for raw in ws:
                        await self._handle_message(raw)
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning("User WS disconnected: %s — reconnecting in %.1fs", exc, backoff)
            finally:
                self._ws = None

            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def subscribe_markets(self, condition_ids: list[str]) -> None:
        """Add condition IDs to the subscription. Sends updated list if connected."""
        new = [c for c in condition_ids if c not in self._condition_ids]
        if not new:
            return
        self._condition_ids.update(new)
        if self._ws:
            await self._send_subscribe(self._ws, list(self._condition_ids))

    async def unsubscribe_markets(self, condition_ids: list[str]) -> None:
        """Remove condition IDs and send updated subscription."""
        self._condition_ids.difference_update(condition_ids)
        if self._ws:
            await self._send_subscribe(self._ws, list(self._condition_ids))

    async def _send_subscribe(self, ws, condition_ids: list[str]) -> None:
        await ws.send(json.dumps({
            "action": "subscribe",
            "markets": condition_ids,
        }))

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("User WS: non-JSON message: %r", raw)
            return

        if not isinstance(msg, list):
            msg = [msg]

        for event in msg:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type", "")
            if event_type == "trade":
                await self._emit_fill(event)
            elif event_type == "order_cancelled":
                await self._emit_cancel(event)
            elif event_type in ("order_placement", "order_ack"):
                await self._emit_ack(event)

    async def _emit_fill(self, event: dict) -> None:
        try:
            fill = FillEvent(
                order_id=event["order_id"],
                token_id=event["asset_id"],
                market_id=event.get("market_id", ""),
                side=event["side"],
                price=float(event["price"]),
                size=float(event["size"]),
                maker_taker=event.get("maker_taker", "TAKER"),
                strategy=event.get("strategy", ""),
                fill_timestamp=float(event.get("timestamp", time.time())),
            )
            await self._fill_queue.put(fill)
        except (KeyError, ValueError) as exc:
            log.warning("User WS: malformed fill event: %s — %s", event, exc)

    async def _emit_cancel(self, event: dict) -> None:
        try:
            cancel = CancelEvent(
                order_id=event["order_id"],
                token_id=event.get("asset_id", ""),
            )
            await self._cancel_queue.put(cancel)
        except KeyError as exc:
            log.warning("User WS: malformed cancel event: %s — %s", event, exc)

    async def _emit_ack(self, event: dict) -> None:
        try:
            ack = OrderAckEvent(
                order_id=event["order_id"],
                token_id=event.get("asset_id", ""),
            )
            await self._ack_queue.put(ack)
        except KeyError as exc:
            log.warning("User WS: malformed ack event: %s — %s", event, exc)
