"""
Market WebSocket gateway.

FR-105: Subscribe to target token IDs via the market channel.
FR-108: Exponential backoff reconnect (1s → 2s → 4s → … → 30s cap).
FR-109: Dynamic subscribe/unsubscribe without full reconnect.

Book state management:
  - "book" events (full snapshot): replace the entire local book for that token.
  - "price_change" events (delta): apply individual level changes from the
    `changes` array (side/price/size; size=0 means level removed).
  The gateway maintains per-token local books so that every BookEvent emitted
  to the queue is always a complete, self-consistent snapshot.
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
        # Per-token local books: token_id → (bids, asks)
        # Each side is a dict keyed by price string → size float.
        self._local_books: dict[str, tuple[dict[str, float], dict[str, float]]] = {}
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
            except Exception as exc:
                log.warning("Market WS error: %s — reconnecting in %.1fs", exc, backoff)
            finally:
                self._ws = None

            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def send(self, message: str) -> None:
        """Send a raw message to the websocket if connected."""
        if self._ws:
            await self._ws.send(message)

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
        for tid in removed:
            self._local_books.pop(tid, None)
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

        # Market channel sends either a single dict or a list of event dicts
        if isinstance(msg, list):
            events = msg
        elif isinstance(msg, dict):
            events = [msg]
        else:
            return

        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("event_type") or event.get("type")
            if event_type in ("book", "price_change"):
                await self._apply_and_emit(event, event_type)
            elif event_type:
                log.debug("Market WS: unhandled event_type=%r", event_type)

    async def _apply_and_emit(self, msg: dict, event_type: str) -> None:
        """Update the local book from this event, then emit a full BookEvent."""
        token_id = msg.get("asset_id") or msg.get("token_id", "")
        if not token_id:
            return

        if event_type == "book":
            # Full snapshot: replace the entire local book
            bids: dict[str, float] = {}
            asks: dict[str, float] = {}
            for b in msg.get("bids", []):
                size = float(b["size"])
                if size > 0:
                    bids[b["price"]] = size
            for a in msg.get("asks", []):
                size = float(a["size"])
                if size > 0:
                    asks[a["price"]] = size
            self._local_books[token_id] = (bids, asks)

        elif event_type == "price_change":
            # Delta: apply individual level changes
            if token_id not in self._local_books:
                # No snapshot yet — cannot apply delta; skip and wait for book event
                return
            bids, asks = self._local_books[token_id]
            for change in msg.get("changes", []):
                side = change.get("side", "")
                price_str = str(change.get("price", ""))
                size = float(change.get("size", 0))
                if side == "BUY":
                    if size == 0:
                        bids.pop(price_str, None)
                    else:
                        bids[price_str] = size
                elif side == "SELL":
                    if size == 0:
                        asks.pop(price_str, None)
                    else:
                        asks[price_str] = size

        # Build sorted PriceLevel lists from the current local book state
        if token_id not in self._local_books:
            return

        bids_dict, asks_dict = self._local_books[token_id]
        sorted_bids = sorted(
            [PriceLevel(price=float(p), size=s) for p, s in bids_dict.items()],
            key=lambda l: -l.price,
        )
        sorted_asks = sorted(
            [PriceLevel(price=float(p), size=s) for p, s in asks_dict.items()],
            key=lambda l: l.price,
        )

        # Sequence gap detection: crossed book signals a missed or out-of-order delta
        if sorted_bids and sorted_asks and sorted_bids[0].price >= sorted_asks[0].price:
            count = self._missed_delta_counts.get(token_id, 0) + 1
            self._missed_delta_counts[token_id] = count
            log.debug("Market WS: crossed book on %s (missed=%d)", token_id, count)
            if count >= self._delta_threshold:
                log.warning("Delta threshold reached for %s — triggering resync", token_id)
                await self._resync_queue.put(token_id)
                self._missed_delta_counts[token_id] = 0
            return

        self._missed_delta_counts[token_id] = 0
        book_event = BookEvent(
            token_id=token_id,
            bids=sorted_bids,
            asks=sorted_asks,
            timestamp=time.time(),
        )
        await self._book_queue.put(book_event)
