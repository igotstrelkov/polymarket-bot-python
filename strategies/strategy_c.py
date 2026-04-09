"""Strategy C: Resolution-Window Sniping (§5.3, FR-155).

Places Post-Only GTD bids at a dynamic offset below the prevailing ask in the
4-to-2-hour pre-resolution window when market certainty > SNIPE_PROB_THRESHOLD.
Entry restricted to near-zero-fee markets (fee_rate_bps < SNIPE_MAX_FEE_BPS).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import Signal
from fees.cache import FeeRateCache
from inventory.manager import InventoryState
from strategies.base import BaseStrategy

_MIN_SPREAD_FOR_ENTRY: float = 0.02   # §5.3.2: spread >= 2¢


@dataclass
class StrategyC(BaseStrategy):
    """Resolution-window sniping with dynamic offset and probability-scaled size."""

    strategy_id: str = field(default="C", init=False)
    enabled: bool = True
    max_exposure: float = 50.0   # SNIPE_MAX_POSITION
    kill_switch_active: bool = False

    prob_threshold: float = 0.90
    max_fee_bps: int = 5
    snipe_min_size: int = 5
    snipe_max_size: int = 20
    resolution_warn_ms: int = 7_200_000     # 2h inner bound — no-entry window
    snipe_entry_window_ms: int = 14_400_000  # 4h outer bound — entry opens
    gtd_resolution_buffer_ms: int = 7_200_000

    # Per-evaluate: current position in this market
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

        # Gate: not negRisk (§5.3.2)
        if market.neg_risk:
            return []

        if not market.accepting_orders:
            return []

        # Gate: fee_rate_bps < SNIPE_MAX_FEE_BPS (FR-155, hard gate, strict <)
        fee_rate_bps = fee_cache.get(market.token_id)
        if fee_rate_bps is None:
            return []
        if fee_rate_bps >= self.max_fee_bps:
            return []

        now_ms = time.time() * 1000

        # Gate: resolution must exist and be within the 4h-to-2h window
        if market.resolution_time is None:
            return []
        res_ms = market.resolution_time.timestamp() * 1000
        time_to_resolution_ms = res_ms - now_ms

        # Inner bound: must be > 2h from resolution (resolution_warn_ms)
        if time_to_resolution_ms <= self.resolution_warn_ms:
            return []
        # Outer bound: must be within 4h of resolution (entry window)
        if time_to_resolution_ms > self.snipe_entry_window_ms:
            return []

        # Gate: position limit
        if self.current_position >= self.max_exposure:
            return []

        # Determine YES probability from book mid
        mid = book.mid()
        if mid is None:
            return []

        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return []

        spread = best_ask - best_bid

        # Gate: spread >= 2¢ in target direction
        if spread < _MIN_SPREAD_FOR_ENTRY:
            return []

        # Gate: probability threshold
        yes_side = mid > self.prob_threshold
        no_side = mid < (1.0 - self.prob_threshold)
        if not yes_side and not no_side:
            return []

        # Dynamic offset: max(0.01, min(0.02, spread × 0.5)) — §5.3.3
        offset = max(0.01, min(0.02, spread * 0.5))

        if yes_side:
            price = best_ask - offset
            prob_certainty = mid
        else:
            # NO side: effective NO ask = 1 - best_bid (yes-book perspective)
            no_ask = 1.0 - best_bid
            price = no_ask - offset
            prob_certainty = 1.0 - mid

        # Size scaling formula — §5.3.3
        scale = (prob_certainty - self.prob_threshold) / (1.0 - self.prob_threshold)
        size = self.snipe_min_size + int(scale * (self.snipe_max_size - self.snipe_min_size))
        size = max(self.snipe_min_size, size)

        # GTD expiry: resolutionTime - buffer + 60
        expiration = int(
            market.resolution_time.timestamp()
            - self.gtd_resolution_buffer_ms // 1000
            + 60
        )

        return [
            Signal(
                token_id=market.token_id,
                side="BUY",
                price=round(price, 4),
                size=size,
                time_in_force="GTD",
                post_only=True,
                expiration=expiration,
                strategy="C",
                fee_rate_bps=fee_rate_bps,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
        ]
