"""
Order Ledger — append-only record of every order lifecycle event.

FR-503: Persist durable state to Postgres (orders, fills, positions, rewards,
        rebates). For minimal deployments persist to encrypted local JSON.
FR-504: On startup, rebuild state from Order Ledger before re-enabling quoting.

Design:
  - In-process append-only store keyed by order_id.
  - Each record is an OrderRecord (immutable snapshot per state transition).
  - replace() creates a new record with updated state; the old record is
    retained in history for postmortems.
  - All timestamps are UTC-aware datetimes.
  - Storage backend is injected (Storage ABC from storage layer).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)


class OrderState(Enum):
    SUBMITTED    = auto()   # sent to CLOB, no ack yet
    ACKNOWLEDGED = auto()   # ack received from User WS / REST
    PARTIALLY_FILLED = auto()
    FILLED       = auto()
    CANCELLED    = auto()
    EXPIRED      = auto()
    REJECTED     = auto()


@dataclass
class OrderRecord:
    order_id: str
    token_id: str
    side: str            # 'BUY' | 'SELL'
    price: float
    size: float
    time_in_force: str   # 'GTC' | 'GTD'
    post_only: bool
    strategy: str        # 'A' | 'B' | 'C'
    fee_rate_bps: int
    neg_risk: bool
    state: OrderState
    created_at: datetime
    updated_at: datetime
    # optional fields populated as lifecycle progresses
    filled_size: float = 0.0
    cancel_reason: str = ""
    extra: dict = field(default_factory=dict)


class OrderLedger:
    """Append-only in-process order ledger.

    Usage:
        ledger = OrderLedger()
        ledger.record_submitted(order_id=..., ...)
        ledger.record_acknowledged(order_id)
        ledger.record_filled(order_id, filled_size=10.0)
        record = ledger.get(order_id)
        open_orders = ledger.open_orders()
    """

    def __init__(self) -> None:
        # Latest record per order_id
        self._records: dict[str, OrderRecord] = {}
        # Full history: order_id → list of records (oldest first)
        self._history: dict[str, list[OrderRecord]] = {}

    # ── Write API ─────────────────────────────────────────────────────────────

    def record_submitted(
        self,
        *,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        time_in_force: str,
        post_only: bool,
        strategy: str,
        fee_rate_bps: int,
        neg_risk: bool,
        extra: dict[str, Any] | None = None,
    ) -> OrderRecord:
        now = datetime.now(tz=timezone.utc)
        rec = OrderRecord(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            time_in_force=time_in_force,
            post_only=post_only,
            strategy=strategy,
            fee_rate_bps=fee_rate_bps,
            neg_risk=neg_risk,
            state=OrderState.SUBMITTED,
            created_at=now,
            updated_at=now,
            extra=extra or {},
        )
        self._upsert(rec)
        log.debug("OrderLedger: submitted %s", order_id)
        return rec

    def record_acknowledged(self, order_id: str) -> OrderRecord | None:
        return self._transition(order_id, OrderState.ACKNOWLEDGED)

    def record_partially_filled(self, order_id: str, filled_size: float) -> OrderRecord | None:
        rec = self._transition(order_id, OrderState.PARTIALLY_FILLED)
        if rec:
            rec.filled_size = filled_size
        return rec

    def record_filled(self, order_id: str, filled_size: float | None = None) -> OrderRecord | None:
        rec = self._transition(order_id, OrderState.FILLED)
        if rec and filled_size is not None:
            rec.filled_size = filled_size
        return rec

    def record_cancelled(self, order_id: str, reason: str = "") -> OrderRecord | None:
        rec = self._transition(order_id, OrderState.CANCELLED)
        if rec:
            rec.cancel_reason = reason
        return rec

    def record_expired(self, order_id: str) -> OrderRecord | None:
        return self._transition(order_id, OrderState.EXPIRED)

    def record_rejected(self, order_id: str, reason: str = "") -> OrderRecord | None:
        rec = self._transition(order_id, OrderState.REJECTED)
        if rec:
            rec.cancel_reason = reason
        return rec

    # ── Read API ──────────────────────────────────────────────────────────────

    def get(self, order_id: str) -> OrderRecord | None:
        return self._records.get(order_id)

    def open_orders(self) -> list[OrderRecord]:
        """Return orders that are not in a terminal state."""
        terminal = {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED}
        return [r for r in self._records.values() if r.state not in terminal]

    def history(self, order_id: str) -> list[OrderRecord]:
        return list(self._history.get(order_id, []))

    def all_records(self) -> list[OrderRecord]:
        return list(self._records.values())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _upsert(self, rec: OrderRecord) -> None:
        self._records[rec.order_id] = rec
        self._history.setdefault(rec.order_id, []).append(rec)

    def _transition(self, order_id: str, new_state: OrderState) -> OrderRecord | None:
        existing = self._records.get(order_id)
        if existing is None:
            log.warning("OrderLedger: transition to %s for unknown order %s", new_state, order_id)
            return None
        # Create a shallow copy with updated state and timestamp
        import dataclasses
        rec = dataclasses.replace(existing, state=new_state, updated_at=datetime.now(tz=timezone.utc))
        self._upsert(rec)
        return rec
