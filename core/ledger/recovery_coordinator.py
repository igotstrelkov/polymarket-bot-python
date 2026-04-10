"""
Recovery Coordinator — rebuilds operational state on startup or reconnect.

FR-502: On any reconnection, query open orders to rebuild Confirmed state.
FR-504: On startup, rebuild state from Postgres/Order Ledger and Data API.

Recovery sequence:
  1. Query open orders from CLOB (get_open_orders()) → rebuild Confirmed state
  2. Load fill/position history from Postgres / Order Ledger
  3. Mark recovery complete; resume quoting

The coordinator tracks the last recovery timestamp and the set of order IDs
that were live at recovery time.  It does NOT re-evaluate Desired state during
recovery — the Quote Engine does that after recovery_complete() is called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.ledger.order_ledger import OrderLedger, OrderState

log = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    success: bool
    recovered_order_ids: list[str]
    recovered_at: datetime
    error: str = ""


class RecoveryCoordinator:
    """Orchestrates state reconstruction after startup or WS reconnect.

    Usage (called by the bot entrypoint / Liveness Manager):
        coordinator = RecoveryCoordinator(order_ledger)
        result = await coordinator.recover(clob_client)
        if result.success:
            # Confirmed state is rebuilt; safe to re-enable quoting
            confirmed_orders = coordinator.confirmed_order_ids()
    """

    def __init__(self, order_ledger: OrderLedger) -> None:
        self._ledger = order_ledger
        self._last_result: RecoveryResult | None = None
        self._resyncing: bool = False

    # ── Recovery lifecycle ────────────────────────────────────────────────────

    async def recover(self, clob_client: Any) -> RecoveryResult:
        """Execute the full recovery sequence.

        1. Set resyncing=True (blocks new quote placement via is_resyncing()).
        2. Fetch open orders from CLOB.
        3. Mark all previously SUBMITTED/ACKNOWLEDGED orders in the ledger
           as CANCELLED if they no longer appear in the open-orders response.
        4. Record surviving open orders as ACKNOWLEDGED.
        5. Set resyncing=False and return the result.
        """
        self._resyncing = True
        log.info("RecoveryCoordinator: starting recovery")

        try:
            open_orders: list[dict] = await clob_client.get_open_orders()
        except Exception as exc:
            log.exception("RecoveryCoordinator: failed to fetch open orders")
            self._resyncing = False
            result = RecoveryResult(
                success=False,
                recovered_order_ids=[],
                recovered_at=datetime.now(tz=timezone.utc),
                error=str(exc),
            )
            self._last_result = result
            return result

        # Build set of order IDs still open on the exchange
        live_ids: set[str] = {o["id"] for o in open_orders if "id" in o}

        # Reconcile ledger
        recovered: list[str] = []
        for rec in self._ledger.open_orders():
            if rec.order_id in live_ids:
                self._ledger.record_acknowledged(rec.order_id)
                recovered.append(rec.order_id)
            else:
                # No longer on the exchange — treat as cancelled/expired
                self._ledger.record_cancelled(rec.order_id, reason="not_in_open_orders_on_recovery")

        # Record any orders that exist on the exchange but not in our ledger
        # (e.g. placed before a previous crash) — submit a stub record
        for raw in open_orders:
            oid = raw.get("id", "")
            if oid and oid not in {r.order_id for r in self._ledger.all_records()}:
                self._ledger.record_submitted(
                    order_id=oid,
                    token_id=raw.get("asset_id", ""),
                    side=raw.get("side", ""),
                    price=float(raw.get("price", 0)),
                    size=float(raw.get("original_size", 0)),
                    time_in_force=raw.get("time_in_force", "GTC"),
                    post_only=bool(raw.get("maker_amount", 0)),
                    strategy="",   # unknown — pre-crash order
                    fee_rate_bps=0,
                    neg_risk=False,
                    extra={"source": "recovery"},
                )
                self._ledger.record_acknowledged(oid)
                recovered.append(oid)

        self._resyncing = False
        result = RecoveryResult(
            success=True,
            recovered_order_ids=recovered,
            recovered_at=datetime.now(tz=timezone.utc),
        )
        self._last_result = result
        log.info(
            "RecoveryCoordinator: recovery complete; %d orders confirmed live",
            len(recovered),
        )
        return result

    # ── State queries ─────────────────────────────────────────────────────────

    def is_resyncing(self) -> bool:
        """True while recovery is in progress — Quote Engine must not diff during this period."""
        return self._resyncing

    def confirmed_order_ids(self) -> list[str]:
        """Order IDs confirmed live after the last successful recovery."""
        if self._last_result and self._last_result.success:
            return list(self._last_result.recovered_order_ids)
        return []

    def last_recovery(self) -> RecoveryResult | None:
        return self._last_result
