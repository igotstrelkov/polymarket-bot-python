"""Strategy A: Event-Driven Market Making (§5.1, FR-153/154).

10-gate entry with value-weighted inventory skew management and GTD/GTC duration logic.
Quote positioning improves by one tick; skew offset applied when |skew| >= threshold.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import Signal
from fees.cache import FeeRateCache
from fees.calculator import passes_strategy_a_gate
from inventory.manager import InventoryState, quote_offset_ticks, value_weighted_skew
from strategies.base import BaseStrategy


@dataclass
class StrategyA(BaseStrategy):
    """Event-driven market making with 10 entry gates and inventory skew management."""

    strategy_id: str = field(default="A", init=False)
    enabled: bool = True
    max_exposure: float = 100.0
    kill_switch_active: bool = False

    base_spread: float = 0.04
    cost_floor: float = 0.01
    order_size: int = 10
    resolution_warn_ms: int = 7_200_000        # Gate 1/7: 2h no-entry window
    gtd_within_ms: int = 21_600_000            # 6h: switch from GTC to GTD
    gtd_resolution_buffer_ms: int = 7_200_000  # buffer for GTD expiry formula
    inventory_skew_threshold: float = 0.65
    inventory_halt_threshold: float = 0.80
    inventory_skew_multiplier: int = 3

    # Per-market current position — caller updates before evaluate()
    current_position: float = 0.0

    async def evaluate(
        self,
        market: MarketCapabilityModel,
        book: BookStateStore,
        inventory: InventoryState,
        fee_cache: FeeRateCache,
    ) -> list[Signal]:
        if not self.enabled or self.kill_switch_active:
            return []

        # Gate 8: accepting_orders
        if not market.accepting_orders:
            return []

        # Gate 9: seconds_delay
        if market.seconds_delay != 0:
            return []

        now_ms = time.time() * 1000

        # Gate 1/7: not within resolution warning window (FR-214)
        time_to_resolution_ms: float = math.inf
        if market.resolution_time is not None:
            res_ms = market.resolution_time.timestamp() * 1000
            time_to_resolution_ms = res_ms - now_ms
            if time_to_resolution_ms <= self.resolution_warn_ms:
                return []

        # Gate 10: sports market — skip if game_start has already passed
        time_to_game_ms: float = math.inf
        if market.game_start_time is not None:
            gst_ms = market.game_start_time.timestamp() * 1000
            time_to_game_ms = gst_ms - now_ms
            if time_to_game_ms <= 0:
                return []

        # Gate 5: position limit
        if self.current_position >= self.max_exposure:
            return []

        # Fee cache lookup — defer one cycle on miss
        fee_rate_bps = fee_cache.get(market.token_id)
        if fee_rate_bps is None:
            return []

        # Gate 4: midpoint range [0.05, 0.95]
        mid = book.mid()
        if mid is None or not (0.05 <= mid <= 0.95):
            return []

        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return []

        observed_spread = best_ask - best_bid

        # Gate 2 + Gate 3: FR-153 (min spread) AND FR-154 (3x spread when fee > 100bps)
        if not passes_strategy_a_gate(
            fee_rate_bps=fee_rate_bps,
            observed_spread=observed_spread,
            base_spread=self.base_spread,
            cost_floor=self.cost_floor,
        ):
            return []

        # Inventory halt check: do not quote when |skew| >= halt threshold
        skew = value_weighted_skew(inventory)
        if abs(skew) >= self.inventory_halt_threshold:
            return []

        # Quote positioning: improve by one tick from best bid/ask
        tick = market.tick_size
        bid_price = best_bid + tick
        ask_price = best_ask - tick

        # Apply inventory skew offset when |skew| exceeds threshold
        if abs(skew) >= self.inventory_skew_threshold:
            offset = quote_offset_ticks(skew, self.inventory_skew_multiplier)
            bid_price -= offset * tick
            ask_price += offset * tick

        # Gate 6: post-adjustment quoted spread must still exceed 3¢
        if ask_price - bid_price < 0.03:
            return []

        # Clamp to valid probability range
        bid_price = max(0.01, min(0.99, round(bid_price, 4)))
        ask_price = max(0.01, min(0.99, round(ask_price, 4)))

        # Determine order duration
        time_in_force = "GTC"
        expiration: int | None = None

        if math.isfinite(time_to_resolution_ms) and 0 < time_to_resolution_ms <= self.gtd_within_ms:
            time_in_force = "GTD"
            expiration = int(
                market.resolution_time.timestamp()  # type: ignore[union-attr]
                - self.gtd_resolution_buffer_ms // 1000
                + 60
            )
        elif math.isfinite(time_to_game_ms) and 0 < time_to_game_ms <= self.gtd_within_ms:
            time_in_force = "GTD"
            expiration = int(
                market.game_start_time.timestamp()  # type: ignore[union-attr]
                - self.gtd_resolution_buffer_ms // 1000
                + 60
            )

        common = dict(
            token_id=market.token_id,
            size=self.order_size,
            time_in_force=time_in_force,
            post_only=True,
            expiration=expiration,
            strategy="A",
            fee_rate_bps=fee_rate_bps,
            neg_risk=market.neg_risk,
            tick_size=tick,
        )

        return [
            Signal(side="BUY",  price=bid_price, **common),
            Signal(side="SELL", price=ask_price, **common),
        ]
