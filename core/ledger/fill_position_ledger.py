"""
Fill and Position Ledger — persists fills, realized P&L, positions,
and 30-second post-fill markout for Strategy A adverse-selection tracking.

FR-601a: For every Strategy A fill, record mid-price at fill time and schedule
         a 30-second deferred mid-price lookup. Markout = mid_t30 - mid_t0 × side-sign.
         Positive markout = adverse (mid moved against our filled side).
FR-504:  Recovery uses authenticated trade data and Data API positions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class FillRecord:
    fill_id: str
    order_id: str
    token_id: str
    side: str              # 'BUY' | 'SELL'
    price: float
    size: float
    fee_paid: float        # in USDC
    strategy: str
    is_maker: bool         # True = maker (Post-Only executed as maker)
    filled_at: datetime

    # Markout fields (FR-601a) — populated 30s after fill
    mid_at_fill: float | None = None
    mid_at_t30: float | None = None
    markout_30s: float | None = None   # positive = adverse


@dataclass
class PositionRecord:
    token_id: str
    shares: float        # net shares held (positive = long)
    avg_entry_price: float
    realized_pnl: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class FillAndPositionLedger:
    """In-process fill and position ledger.

    Positions are maintained per token_id. Each fill updates the running
    net position and realized P&L. The markout is recorded separately via
    record_markout() called by a deferred 30-second task.
    """

    def __init__(self) -> None:
        self._fills: dict[str, FillRecord] = {}   # fill_id → FillRecord
        self._fills_by_order: dict[str, list[str]] = {}  # order_id → [fill_id]
        self._positions: dict[str, PositionRecord] = {}  # token_id → PositionRecord
        # Pending markout lookups: fill_id → (fill_at timestamp, token_id, side)
        self._pending_markout: dict[str, tuple[datetime, str, str]] = {}

    # ── Fill recording ────────────────────────────────────────────────────────

    def record_fill(
        self,
        *,
        fill_id: str,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee_paid: float,
        strategy: str,
        is_maker: bool,
        mid_at_fill: float | None = None,
    ) -> FillRecord:
        now = datetime.now(tz=timezone.utc)
        rec = FillRecord(
            fill_id=fill_id,
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            fee_paid=fee_paid,
            strategy=strategy,
            is_maker=is_maker,
            filled_at=now,
            mid_at_fill=mid_at_fill,
        )
        self._fills[fill_id] = rec
        self._fills_by_order.setdefault(order_id, []).append(fill_id)
        self._update_position(token_id, side, price, size)

        # FR-601a: schedule markout tracking for Strategy A fills
        if strategy == "A" and mid_at_fill is not None:
            self._pending_markout[fill_id] = (now, token_id, side)
            log.debug("FillPositionLedger: markout pending for fill %s", fill_id)

        log.debug(
            "FillPositionLedger: fill %s %s %s qty=%.2f price=%.4f maker=%s",
            fill_id, side, token_id, size, price, is_maker,
        )
        return rec

    def record_markout(self, fill_id: str, mid_at_t30: float) -> FillRecord | None:
        """FR-601a: record the 30-second post-fill mid-price and compute markout."""
        rec = self._fills.get(fill_id)
        if rec is None:
            log.warning("FillPositionLedger: markout for unknown fill %s", fill_id)
            return None

        rec.mid_at_t30 = mid_at_t30

        if rec.mid_at_fill is not None:
            # side-sign: BUY = +1 (adverse if mid goes up after we buy), SELL = -1
            side_sign = 1.0 if rec.side.upper() == "BUY" else -1.0
            rec.markout_30s = (mid_at_t30 - rec.mid_at_fill) * side_sign

        self._pending_markout.pop(fill_id, None)
        log.debug(
            "FillPositionLedger: markout fill=%s markout_30s=%.5f",
            fill_id, rec.markout_30s or 0.0,
        )
        return rec

    # ── Position updates ──────────────────────────────────────────────────────

    def _update_position(self, token_id: str, side: str, price: float, size: float) -> None:
        pos = self._positions.get(token_id)
        if pos is None:
            pos = PositionRecord(
                token_id=token_id,
                shares=0.0,
                avg_entry_price=price,
            )
            self._positions[token_id] = pos

        now = datetime.now(tz=timezone.utc)

        if side.upper() == "BUY":
            total_cost = pos.shares * pos.avg_entry_price + size * price
            pos.shares += size
            pos.avg_entry_price = total_cost / pos.shares if pos.shares > 0 else price
        elif side.upper() == "SELL":
            realized = (price - pos.avg_entry_price) * size
            pos.realized_pnl += realized
            pos.shares -= size
            if pos.shares < 0:
                pos.shares = 0.0  # floor at zero; short positions not modeled

        pos.last_updated = now

    # ── Read API ──────────────────────────────────────────────────────────────

    def get_fill(self, fill_id: str) -> FillRecord | None:
        return self._fills.get(fill_id)

    def fills_for_order(self, order_id: str) -> list[FillRecord]:
        return [self._fills[fid] for fid in self._fills_by_order.get(order_id, []) if fid in self._fills]

    def get_position(self, token_id: str) -> PositionRecord | None:
        return self._positions.get(token_id)

    def all_positions(self) -> list[PositionRecord]:
        return list(self._positions.values())

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    def pending_markout_fill_ids(self) -> list[str]:
        """Fill IDs still awaiting their 30s mid-price lookup."""
        return list(self._pending_markout.keys())

    def fill_count(self, strategy: str | None = None) -> int:
        if strategy is None:
            return len(self._fills)
        return sum(1 for r in self._fills.values() if r.strategy == strategy)

    def all_fills(self) -> list[FillRecord]:
        return list(self._fills.values())
