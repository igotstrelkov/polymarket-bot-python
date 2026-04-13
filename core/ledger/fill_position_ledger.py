"""
Fill and Position Ledger — persists fills, realized P&L, positions,
and 30-second post-fill markout for Strategy A adverse-selection tracking.

FR-601a: For every Strategy A fill, record mid-price at fill time and schedule
         a 30-second deferred mid-price lookup. Markout = mid_t30 - mid_t0 × side-sign.
         Positive markout = adverse (mid moved against our filled side).
FR-504:  Recovery uses authenticated trade data and Data API positions.

DRY_RUN fill simulation (§11.4 Step 2):
  maybe_simulate_fill() generates a fake fill with probability 0.3 at the quoted
  price. All simulated fills carry simulated=True. Markout scheduling (FR-601a)
  is NEVER applied to simulated fills — they must not mix with live fill markouts.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Separate structured logger for paper-trading JSON events consumed by
# scripts/paper_trading_report.py.  Output is captured when the bot is started
# with a JSON-line handler (e.g. logging.FileHandler on a *.jsonl file).
_trade_log = logging.getLogger("polymarket.trades")

# Fixed fill simulation probability per §11.4 Step 2 scope note.
_SIM_PROBABILITY = 0.3


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

    # Markout fields (FR-601a) — populated 30s after fill; live fills only
    mid_at_fill: float | None = None
    mid_at_t30: float | None = None
    markout_30s: float | None = None   # positive = adverse

    # DRY_RUN simulation flag — simulated fills never enter markout calculations
    simulated: bool = False


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

    Simulated fills (DRY_RUN mode) are stored and tracked for paper-trading
    reports but are excluded from FR-601a markout scheduling.
    """

    def __init__(self) -> None:
        self._fills: dict[str, FillRecord] = {}   # fill_id → FillRecord
        self._fills_by_order: dict[str, list[str]] = {}  # order_id → [fill_id]
        self._positions: dict[str, PositionRecord] = {}  # token_id → PositionRecord
        # Pending markout lookups: fill_id → (fill_at timestamp, token_id, side)
        # Only live (non-simulated) Strategy A fills are ever inserted here.
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
        simulated: bool = False,
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
            simulated=simulated,
        )
        self._fills[fill_id] = rec
        self._fills_by_order.setdefault(order_id, []).append(fill_id)
        self._update_position(token_id, side, price, size)

        # FR-601a: schedule markout ONLY for live Strategy A fills.
        # Simulated fills are explicitly excluded — never mix with live markouts.
        if strategy == "A" and mid_at_fill is not None and not simulated:
            self._pending_markout[fill_id] = (now, token_id, side)
            log.debug("FillPositionLedger: markout pending for fill %s", fill_id)

        log.debug(
            "FillPositionLedger: fill %s %s %s qty=%.2f price=%.4f maker=%s simulated=%s",
            fill_id, side, token_id, size, price, is_maker, simulated,
        )
        return rec

    def record_markout(self, fill_id: str, mid_at_t30: float) -> FillRecord | None:
        """FR-601a: record the 30-second post-fill mid-price and compute markout.

        Only live fills reach this method — simulated fills are never inserted
        into _pending_markout, so they cannot appear here.
        """
        rec = self._fills.get(fill_id)
        if rec is None:
            log.warning("FillPositionLedger: markout for unknown fill %s", fill_id)
            return None

        if rec.simulated:
            log.warning(
                "FillPositionLedger: record_markout called for simulated fill %s — ignored",
                fill_id,
            )
            return rec

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

    # ── DRY_RUN fill simulation ───────────────────────────────────────────────

    def maybe_simulate_fill(
        self,
        *,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        strategy: str,
        mid_at_fill: float | None = None,
        _rng: random.Random | None = None,
    ) -> FillRecord | None:
        """DRY_RUN fill simulator — 30% probability of generating a simulated fill.

        ⚠️ SCOPE LIMIT: fill probability (0.3) is intentionally arbitrary.
        This is scaffolding for state-machine validation only. Do NOT use
        simulated P&L to decide whether to deploy capital.

        Simulated fills are tagged simulated=True and NEVER scheduled for
        markout (FR-601a applies to live fills only).

        Args:
            _rng: injectable Random instance for deterministic tests.

        Returns:
            FillRecord if a simulated fill was generated, else None.
        """
        rng = _rng or random
        if rng.random() >= _SIM_PROBABILITY:
            return None

        fill_id = f"sim_{order_id}_{int(time.monotonic() * 1_000_000) % 1_000_000_000}"
        rec = self.record_fill(
            fill_id=fill_id,
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            fee_paid=0.0,       # no real fees in simulation
            strategy=strategy,
            is_maker=True,      # DRY_RUN always simulates maker execution
            mid_at_fill=mid_at_fill,
            simulated=True,
        )

        log.info(
            "DRY_RUN simulated fill: %s %s %s qty=%.2f price=%.4f",
            fill_id, side, token_id, size, price,
        )

        # Emit structured JSON event for paper_trading_report.py
        self._emit_fill_event(rec)
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

    # ── Structured event logging ──────────────────────────────────────────────

    @staticmethod
    def _emit_fill_event(rec: FillRecord) -> None:
        """Emit a structured JSON log line for paper_trading_report.py.

        Uses the 'polymarket.trades' logger so callers can route trade events
        to a separate *.jsonl file without polluting the main log.
        """
        pnl_estimate = 0.0  # P&L for a single fill is unknown without full position context

        event = {
            "event_type": "FILL",
            "timestamp": rec.filled_at.timestamp(),
            "fill_id": rec.fill_id,
            "order_id": rec.order_id,
            "token_id": rec.token_id,
            "side": rec.side,
            "price": rec.price,
            "size": rec.size,
            "strategy": rec.strategy,
            "is_maker": rec.is_maker,
            "simulated": rec.simulated,
            "pnl": pnl_estimate,
        }
        _trade_log.info(json.dumps(event))

    # ── Read API ──────────────────────────────────────────────────────────────

    def get_fill(self, fill_id: str) -> FillRecord | None:
        return self._fills.get(fill_id)

    def fills_for_order(self, order_id: str) -> list[FillRecord]:
        return [self._fills[fid] for fid in self._fills_by_order.get(order_id, []) if fid in self._fills]

    def get_position(self, token_id: str) -> PositionRecord | None:
        return self._positions.get(token_id)

    def all_positions(self) -> list[PositionRecord]:
        return list(self._positions.values())

    def total_realized_pnl(self, simulated: bool | None = None) -> float:
        """Sum realized P&L across all positions.

        Args:
            simulated: if None, includes all fills; if True/False, filters by
                       whether the positions were driven by simulated fills.
                       Note: positions are shared; this is a best-effort filter
                       based on fill records only.
        """
        if simulated is None:
            return sum(p.realized_pnl for p in self._positions.values())

        # Filter positions that have at least one fill of the requested type
        tokens_of_type = {
            r.token_id
            for r in self._fills.values()
            if r.simulated is simulated
        }
        return sum(
            p.realized_pnl
            for p in self._positions.values()
            if p.token_id in tokens_of_type
        )

    def pending_markout_fill_ids(self) -> list[str]:
        """Fill IDs still awaiting their 30s mid-price lookup (live fills only)."""
        return list(self._pending_markout.keys())

    def fill_count(self, strategy: str | None = None, simulated: bool | None = None) -> int:
        """Count fills, optionally filtered by strategy and/or simulated flag."""
        fills = self._fills.values()
        if strategy is not None:
            fills = (r for r in fills if r.strategy == strategy)  # type: ignore[assignment]
        if simulated is not None:
            fills = (r for r in fills if r.simulated is simulated)  # type: ignore[assignment]
        return sum(1 for _ in fills)

    def all_fills(self, simulated: bool | None = None) -> list[FillRecord]:
        """Return all fill records, optionally filtered by simulated flag."""
        if simulated is None:
            return list(self._fills.values())
        return [r for r in self._fills.values() if r.simulated is simulated]
