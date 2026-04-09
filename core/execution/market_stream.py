"""
Market WebSocket gateway.

FR-105: Subscribe to target token IDs via the market channel.
FR-108: Exponential backoff reconnect (1s → 2s → 4s → … → 30s cap).
FR-109: Dynamic subscribe/unsubscribe without full reconnect.
"""

import asyncio
import json
import logging
import time

import websockets

from core.execution.types import BookEvent, PriceLevel

log = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0


class MarketStreamGateway:
    """Streams public order book deltas from the Polymarket market channel.

    Emits BookEvent to book_queue. Tracks per-token missed delta counts and
    enqueues resync triggers when the count reaches BOOK_RESYNC_DELTA_THRESHOLD.
    """

    def __init__(
        self,
        book_queue: asyncio.Queue,
        resync_queue: asyncio.Queue,
        delta_threshold: int = 5,
    ) -> None:
        self._book_queue = book_queue
        self._resync_queue = resync_queue
        self._delta_threshold = delta_threshold
        self._subscribed: set[str] = set()
        self._missed_delta_counts: dict[str, int] = {}
        self._ws = None
        self._running = False

    async def connect(self) -> None:
        """Connect and begin streaming. Reconnects with exponential backoff."""
        self._running = True
        backoff = _BACKOFF_INITIAL
        while self._running:
            try:
                async with websockets.connect(MARKET_WS_URL) as ws:
                    self._ws = ws
                    backoff = _BACKOFF_INITIAL  # reset on successful connect
                    log.info("Market WS connected")
                    if self._subscribed:
                        await self._send_subscribe(ws, list(self._subscribed))
                    async for raw in ws:
                        await self._handle_message(raw)
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning("Market WS disconnected: %s — reconnecting in %.1fs", exc, backoff)
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

    async def subscribe(self, token_ids: list[str]) -> None:
        """Add tokens to the subscription set. No reconnect required (FR-109)."""
        new = [t for t in token_ids if t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws:
            await self._send_subscribe(self._ws, new)

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Remove tokens from the subscription set. No reconnect required (FR-109)."""
        removed = [t for t in token_ids if t in self._subscribed]
        if not removed:
            return
        self._subscribed.difference_update(removed)
        if self._ws:
            await self._ws.send(json.dumps({
                "action": "unsubscribe",
                "assets_ids": removed,
            }))

    async def _send_subscribe(self, ws, token_ids: list[str]) -> None:
        await ws.send(json.dumps({
            "action": "subscribe",
            "assets_ids": token_ids,
        }))

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("Market WS: non-JSON message: %r", raw)
            return

        if not isinstance(msg, dict):
            return

        event_type = msg.get("event_type") or msg.get("type")
        if event_type in ("book", "price_change"):
            await self._emit_book_event(msg)

    async def _emit_book_event(self, msg: dict) -> None:
        token_id = msg.get("asset_id") or msg.get("token_id", "")

        bids = [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in msg.get("bids", [])
        ]
        asks = [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in msg.get("asks", [])
        ]

        # Sequence gap detection via crossed/empty book as ordering anomaly
        if bids and asks and bids[0].price >= asks[0].price:
            count = self._missed_delta_counts.get(token_id, 0) + 1
            self._missed_delta_counts[token_id] = count
            log.debug("Market WS: crossed book on %s (missed=%d)", token_id, count)
            if count >= self._delta_threshold:
                log.warning("Delta threshold reached for %s — triggering resync", token_id)
                await self._resync_queue.put(token_id)
                self._missed_delta_counts[token_id] = 0
            return

        self._missed_delta_counts[token_id] = 0
        event = BookEvent(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )
        await self._book_queue.put(event)
