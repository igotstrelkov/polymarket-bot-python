"""
BookStateStore — per-token in-memory order book.

§5.1.2 resync policy:
- resyncing=True blocks new order placements (checked by QuoteEngine / ExecutionActor)
- start_resync() checks the three escalation conditions; returns True when any fires
- complete_resync() atomically replaces the book from a REST response and clears the flag
- Pre-resync in-flight acks update Confirmed state during the window but are NOT
  re-evaluated against Desired state until complete_resync() returns
"""

import logging
import time
from dataclasses import dataclass, field

from config.settings import Settings
from core.execution.types import BookEvent, PriceLevel

log = logging.getLogger(__name__)


@dataclass
class BookStateStore:
    """In-memory order book for a single token."""

    token_id: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    last_update_ts: float = field(default_factory=time.time)
    last_mid: float | None = None   # mid AFTER the most recent update
    resyncing: bool = False
    missed_delta_count: int = 0

    # Mid price from the update BEFORE the most recent one. Used by
    # start_resync() to measure how far the mid has moved across the last delta.
    _prev_mid: float | None = field(default=None, repr=False)

    def update(self, event: BookEvent) -> None:
        """Apply an incremental book event."""
        self._prev_mid = self.last_mid  # save previous mid before replacing
        self.bids = sorted(event.bids, key=lambda l: -l.price)
        self.asks = sorted(event.asks, key=lambda l: l.price)
        self.last_mid = self.mid()      # current mid after update
        self.last_update_ts = event.timestamp

    async def start_resync(self, ws_gap_ms: int, settings: Settings) -> bool:
        """Mark book as resyncing. Return True (escalation) if any condition fires.

        Escalation means the caller must cancel all active quotes before the
        REST resync call completes.

        Three escalation conditions (§5.1.2):
        1. Mid moved > BOOK_RESYNC_CANCEL_MID_PCT since last WS update
        2. Spread > BOOK_RESYNC_CANCEL_SPREAD_TICKS ticks
        3. ws_gap_ms > BOOK_RESYNC_CANCEL_GAP_MS
        """
        self.resyncing = True
        escalate = False

        # Condition 3: WS gap duration
        if ws_gap_ms > settings.BOOK_RESYNC_CANCEL_GAP_MS:
            log.warning(
                "Resync escalation (gap): token=%s gap_ms=%d > %d",
                self.token_id, ws_gap_ms, settings.BOOK_RESYNC_CANCEL_GAP_MS,
            )
            escalate = True

        current_mid = self.mid()

        # Condition 1: mid price movement since the previous WS update
        if self._prev_mid is not None and current_mid is not None and self._prev_mid != 0:
            mid_move_pct = abs(current_mid - self._prev_mid) / self._prev_mid * 100
            if mid_move_pct > settings.BOOK_RESYNC_CANCEL_MID_PCT:
                log.warning(
                    "Resync escalation (mid move): token=%s prev=%.4f cur=%.4f move=%.2f%% > %.2f%%",
                    self.token_id, self._prev_mid, current_mid,
                    mid_move_pct, settings.BOOK_RESYNC_CANCEL_MID_PCT,
                )
                escalate = True

        # Condition 2: spread width — requires tick_size from caller; use default 0.01
        # The caller may pass tick_size via settings if needed; for now detect via
        # the spread_ticks() helper using a sentinel tick_size of 0.01
        # (actual tick_size is per-market and lives in MarketCapabilityModel)
        spread = self.spread_ticks(tick_size=0.01)
        if spread > settings.BOOK_RESYNC_CANCEL_SPREAD_TICKS:
            log.warning(
                "Resync escalation (spread): token=%s spread_ticks=%d > %d",
                self.token_id, spread, settings.BOOK_RESYNC_CANCEL_SPREAD_TICKS,
            )
            escalate = True

        return escalate

    async def complete_resync(self, rest_book: dict) -> None:
        """Atomically replace the full book from a REST response.

        rest_book format: {"bids": [{"price": ..., "size": ...}, ...],
                           "asks": [{"price": ..., "size": ...}, ...]}
        Clears resyncing=False and resets missed_delta_count.
        """
        bids = sorted(
            [PriceLevel(price=float(b["price"]), size=float(b["size"]))
             for b in rest_book.get("bids", [])],
            key=lambda l: -l.price,
        )
        asks = sorted(
            [PriceLevel(price=float(a["price"]), size=float(a["size"]))
             for a in rest_book.get("asks", [])],
            key=lambda l: l.price,
        )
        self.bids = bids
        self.asks = asks
        self.last_mid = self.mid()
        self.last_update_ts = time.time()
        self.missed_delta_count = 0
        self.resyncing = False
        log.info("Book resync complete for %s", self.token_id)

    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def mid(self) -> float | None:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def spread_ticks(self, tick_size: float) -> int:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None or tick_size <= 0:
            return 0
        return round((ba - bb) / tick_size)
