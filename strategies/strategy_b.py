"""Strategy B: Penny Option Scooping (§5.2).

Purchases shares priced at $0.001–$0.03. Most expire worthless; rare hits deliver
100x–500x returns. Uses taker-style limit orders (not Post-Only). Budget-capped per
trade and overall.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import Signal
from fees.cache import FeeRateCache
from inventory.manager import InventoryState
from strategies.base import BaseStrategy

_ONE_DAY_MS: int = 86_400 * 1_000


@dataclass
class StrategyB(BaseStrategy):
    """Penny option scooping: buy shares priced $0.001–$0.03."""

    strategy_id: str = field(default="B", init=False)
    enabled: bool = True
    max_exposure: float = 200.0   # PENNY_MAX_TOTAL
    kill_switch_active: bool = False

    penny_min_price: float = 0.001
    penny_max_price: float = 0.03
    penny_budget: float = 5.0      # max USDC spend per trade

    # Per-evaluate: total USDC already deployed in penny positions
    current_total_position: float = 0.0

    async def evaluate(
        self,
        market: MarketCapabilityModel,
        book: BookStateStore,
        inventory: InventoryState,
        fee_cache: FeeRateCache,
    ) -> list[Signal]:
        if not self.enabled or self.kill_switch_active:
            return []

        if not market.accepting_orders:
            return []

        # Budget cap: do not exceed PENNY_MAX_TOTAL across all penny positions
        if self.current_total_position >= self.max_exposure:
            return []

        now_ms = time.time() * 1000

        # Resolution must be at least 24 hours away (§5.2.2)
        if market.resolution_time is not None:
            res_ms = market.resolution_time.timestamp() * 1000
            if res_ms - now_ms < _ONE_DAY_MS:
                return []

        # Price gate: best ask must be within the penny range
        best_ask = book.best_ask()
        if best_ask is None:
            return []
        if not (self.penny_min_price <= best_ask <= self.penny_max_price):
            return []

        # Size: spend up to penny_budget at the ask price (integer shares, minimum 1)
        size = int(self.penny_budget / best_ask)
        size = max(1, size)

        fee_rate_bps = fee_cache.get(market.token_id) or 0

        return [
            Signal(
                token_id=market.token_id,
                side="BUY",
                price=best_ask,
                size=size,
                time_in_force="GTC",
                post_only=False,   # Strategy B is taker (not Post-Only)
                expiration=None,
                strategy="B",
                fee_rate_bps=fee_rate_bps,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
        ]
