"""Quote Engine — aggregates strategy signals and applies reward constraints.

FR-402–405: for reward-eligible markets, prices are clamped within
rewardsMaxSpread of adjustedMidpoint and sizes are raised to rewardsMinSize.

Output: list[OrderIntent] passed to the Order Diff Actor.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import OrderIntent, Signal
from fees.cache import FeeRateCache
from inventory.manager import InventoryState
from strategies.base import BaseStrategy


@dataclass
class QuoteEngine:
    """Evaluates all strategies and merges their signals into desired-order state."""

    strategies: list[BaseStrategy] = field(default_factory=list)

    def _apply_reward_constraints(
        self,
        signal: Signal,
        market: MarketCapabilityModel,
    ) -> Signal:
        """Apply FR-402–405 reward constraints to a signal.

        FR-402: quote price must be within rewardsMaxSpread/2 of adjustedMidpoint.
        FR-403: order size must meet or exceed rewardsMinSize.
        FR-404: when adjustedMidpoint < 0.10, both YES and NO sides required
                (enforced at the strategy level, not here).
        FR-405: target inner ticks closest to adjustedMidpoint (price clamping
                pulls toward the midpoint naturally).
        """
        if market.adjusted_midpoint is None or market.rewards_max_spread is None:
            return signal

        adj_mid = market.adjusted_midpoint
        half_spread = market.rewards_max_spread / 2.0
        lower = adj_mid - half_spread
        upper = adj_mid + half_spread

        price = signal.price
        if signal.side == "BUY":
            # Bid must not exceed upper bound (FR-402); clamped toward mid
            price = max(lower, min(price, upper))
        else:
            # Ask must not go below lower bound; clamped toward mid
            price = max(lower, min(price, upper))

        size = signal.size
        if market.rewards_min_size is not None:
            size = max(size, market.rewards_min_size)

        return dataclasses.replace(signal, price=price, size=size)

    async def compute(
        self,
        market: MarketCapabilityModel,
        book: BookStateStore,
        inventory: InventoryState,
        fee_cache: FeeRateCache,
    ) -> list[OrderIntent]:
        """Evaluate all active strategies and return the merged desired-order set.

        Strategies that are disabled or kill-switched are skipped. Reward constraints
        are applied for reward-eligible markets before conversion to OrderIntent.
        """
        intents: list[OrderIntent] = []

        for strategy in self.strategies:
            if not strategy.enabled or strategy.kill_switch_active:
                continue

            signals: list[Signal] = await strategy.evaluate(
                market, book, inventory, fee_cache
            )

            for sig in signals:
                sig = self._apply_reward_constraints(sig, market)

                intents.append(
                    OrderIntent(
                        token_id=sig.token_id,
                        side=sig.side,
                        price=sig.price,
                        size=sig.size,
                        time_in_force=sig.time_in_force,
                        post_only=sig.post_only,
                        expiration=sig.expiration,
                        strategy=sig.strategy,
                        fee_rate_bps=sig.fee_rate_bps,
                        neg_risk=sig.neg_risk,
                        tick_size=sig.tick_size,
                    )
                )

        return intents
