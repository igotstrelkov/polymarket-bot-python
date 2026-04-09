"""
Order Diff and Execution Actor (FR-200, §5.1.2).

Two responsibilities:
1. diff() — stateless pure function: computes the minimum mutation set from
   Desired (list[OrderIntent]) → Confirmed (list[ConfirmedOrder]). Never diffs
   Desired → Live. Always works from Confirmed state.

2. ExecutionActor — owns all write traffic to the exchange:
   - Applies mutations (cancel batch, then place batch)
   - Self-cross detection (FR-210a): cancels own opposing order within 1 tick
   - Retry policy on duplicate-ID rejection: 10ms → 25ms → 50ms → reconciliation
   - Adaptive confirm-cancel mode when rejection rate > CANCEL_CONFIRM_THRESHOLD_PCT
   - DRY_RUN: logs mutations without calling the API

Execution order: fire-and-forget cancel → place (default mode).
Confirm-cancel mode: await cancel ack before placing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Union

from config.settings import Settings
from core.execution.types import OrderIntent

log = logging.getLogger(__name__)

_RETRY_DELAYS_S: tuple[float, ...] = (0.010, 0.025, 0.050)


# ── Confirmed order model ─────────────────────────────────────────────────────

@dataclass
class ConfirmedOrder:
    """An order that has been acknowledged via the User WebSocket or open-orders query."""
    order_id: str
    token_id: str
    side: str       # 'BUY' | 'SELL'
    price: float
    size: float
    time_in_force: str
    post_only: bool
    strategy: str


# ── Mutation types ────────────────────────────────────────────────────────────

@dataclass
class PlaceMutation:
    intent: OrderIntent


@dataclass
class CancelMutation:
    order_id: str
    token_id: str
    reason: str = ""   # "replace" | "self_cross" | "no_longer_desired"


OrderMutation = Union[PlaceMutation, CancelMutation]


# ── Diff logic ────────────────────────────────────────────────────────────────

def _price_matches(a: float, b: float, tick_size: float) -> bool:
    """True when two prices are within one tick of each other."""
    return abs(a - b) < tick_size + 1e-9


def _order_matches_intent(order: ConfirmedOrder, intent: OrderIntent) -> bool:
    """True when a confirmed order exactly satisfies a desired OrderIntent.

    An order is considered matching when side, price (within tick tolerance), size,
    time_in_force, and post_only all match. The expiration is not compared here —
    small differences in computed GTD timestamps between cycles are acceptable as long
    as the order is active.
    """
    return (
        order.side == intent.side
        and abs(order.price - intent.price) < intent.tick_size + 1e-9
        and abs(order.size - intent.size) < 1e-6
        and order.time_in_force == intent.time_in_force
        and order.post_only == intent.post_only
    )


def diff(
    desired: list[OrderIntent],
    confirmed: list[ConfirmedOrder],
) -> list[OrderMutation]:
    """Compute the minimum mutation set from Desired → Confirmed.

    Algorithm:
    1. For each desired intent, find the best matching confirmed order.
       - If a perfect match exists: no mutation needed (keep it).
       - If no match: PlaceMutation.
    2. For each confirmed order not matched to any desired intent: CancelMutation.
    3. Self-cross detection (FR-210a): before any PlaceMutation(BUY), if there is a
       confirmed SELL within 1 tick of the intended BUY price (or vice-versa), add a
       CancelMutation for that opposing order first.

    Returns mutations in execution order: cancels before places.
    """
    if not desired and not confirmed:
        return []

    tick_size = desired[0].tick_size if desired else 0.01
    confirmed_remaining = list(confirmed)
    desired_to_place: list[OrderIntent] = []
    confirmed_matched_ids: set[str] = set()

    # Step 1: match desired intents to confirmed orders
    for intent in desired:
        matched = None
        for order in confirmed_remaining:
            if order.token_id == intent.token_id and _order_matches_intent(order, intent):
                matched = order
                break
        if matched is not None:
            confirmed_matched_ids.add(matched.order_id)
            confirmed_remaining = [o for o in confirmed_remaining if o.order_id != matched.order_id]
        else:
            desired_to_place.append(intent)

    # Step 2: confirmed orders not matched → cancel
    cancels: list[CancelMutation] = []
    for order in confirmed:
        if order.order_id not in confirmed_matched_ids:
            cancels.append(CancelMutation(
                order_id=order.order_id,
                token_id=order.token_id,
                reason="no_longer_desired",
            ))

    # Step 3: self-cross detection (FR-210a)
    # For each new BUY: cancel own resting SELL within 1 tick of the buy price
    # For each new SELL: cancel own resting BUY within 1 tick of the sell price
    already_cancelling = {c.order_id for c in cancels}
    extra_cancels: list[CancelMutation] = []

    for intent in desired_to_place:
        opposing_side = "SELL" if intent.side == "BUY" else "BUY"
        for order in confirmed:
            if (
                order.order_id not in already_cancelling
                and order.token_id == intent.token_id
                and order.side == opposing_side
                and _price_matches(order.price, intent.price, tick_size)
            ):
                extra_cancels.append(CancelMutation(
                    order_id=order.order_id,
                    token_id=order.token_id,
                    reason="self_cross",
                ))
                already_cancelling.add(order.order_id)

    all_cancels = cancels + extra_cancels
    all_places = [PlaceMutation(intent=i) for i in desired_to_place]

    # Cancels always before places
    return [*all_cancels, *all_places]


# ── Execution Actor ───────────────────────────────────────────────────────────

@dataclass
class _MarketRejectionTracker:
    """Tracks duplicate-ID rejection rate for adaptive confirm-cancel mode."""
    placements: int = 0
    rejections: int = 0
    window_start_ts: float = field(default_factory=time.time)
    confirm_cancel_mode: bool = False

    def record_placement(self) -> None:
        self._maybe_roll()
        self.placements += 1

    def record_rejection(self) -> None:
        self._maybe_roll()
        self.rejections += 1

    def rejection_rate_pct(self) -> float:
        return (self.rejections / self.placements * 100) if self.placements > 0 else 0.0

    def _maybe_roll(self) -> None:
        """Roll the 60-second window."""
        now = time.time()
        if now - self.window_start_ts >= 60:
            self.placements = 0
            self.rejections = 0
            self.window_start_ts = now


@dataclass
class ExecutionActor:
    """Applies order mutations to the exchange.

    In DRY_RUN mode, mutations are logged but no API calls are made.
    """

    settings: Settings
    # Rejection rate trackers per token_id
    _trackers: dict[str, _MarketRejectionTracker] = field(
        default_factory=dict, repr=False
    )

    def _tracker(self, token_id: str) -> _MarketRejectionTracker:
        if token_id not in self._trackers:
            self._trackers[token_id] = _MarketRejectionTracker()
        return self._trackers[token_id]

    def _update_confirm_cancel_mode(self, token_id: str) -> None:
        t = self._tracker(token_id)
        rate = t.rejection_rate_pct()
        threshold = self.settings.CANCEL_CONFIRM_THRESHOLD_PCT
        if not t.confirm_cancel_mode and rate > threshold:
            t.confirm_cancel_mode = True
            log.warning(
                "ExecutionActor: switching %s to confirm-cancel mode (rejection rate %.1f%% > %.1f%%)",
                token_id, rate, threshold,
            )
        elif t.confirm_cancel_mode and rate <= threshold:
            t.confirm_cancel_mode = False
            log.info(
                "ExecutionActor: %s reverted to fire-and-forget mode (rejection rate %.1f%%)",
                token_id, rate,
            )

    async def _place_with_retry(
        self,
        intent: OrderIntent,
        clob_client,
    ) -> str | None:
        """Attempt to place an order with 10ms/25ms/50ms retry on duplicate-ID rejection.

        Returns the placed order_id on success, None on force-reconciliation trigger.
        """
        tracker = self._tracker(intent.token_id)
        tracker.record_placement()

        for attempt, delay_s in enumerate(_RETRY_DELAYS_S, start=1):
            try:
                order_id = await clob_client.place_order(intent)
                self._update_confirm_cancel_mode(intent.token_id)
                return order_id
            except Exception as exc:
                err_str = str(exc).lower()
                is_duplicate = "duplicate" in err_str or "already exists" in err_str
                if is_duplicate:
                    tracker.record_rejection()
                    self._update_confirm_cancel_mode(intent.token_id)
                    log.debug(
                        "Duplicate-ID rejection on attempt %d/%d for %s %s@%.4f — "
                        "retrying in %dms",
                        attempt, len(_RETRY_DELAYS_S),
                        intent.token_id, intent.side, intent.price,
                        int(delay_s * 1000),
                    )
                    await asyncio.sleep(delay_s)
                else:
                    log.error(
                        "PlaceOrder failed for %s %s@%.4f: %s",
                        intent.token_id, intent.side, intent.price, exc,
                    )
                    return None

        # All retries exhausted → force reconciliation
        log.error(
            "ExecutionActor: 3 duplicate-ID retries exhausted for %s %s@%.4f — "
            "triggering force reconciliation, skipping this cycle",
            intent.token_id, intent.side, intent.price,
        )
        return None

    async def apply(
        self,
        mutations: list[OrderMutation],
        clob_client,
    ) -> dict[str, list[str]]:
        """Apply a list of mutations to the exchange.

        Cancel batch is dispatched first (fire-and-forget in normal mode;
        awaited in confirm-cancel mode). Place batch follows.

        Returns: {"cancelled": [order_ids], "placed": [order_ids]}
        """
        cancels = [m for m in mutations if isinstance(m, CancelMutation)]
        places  = [m for m in mutations if isinstance(m, PlaceMutation)]

        cancelled_ids: list[str] = []
        placed_ids: list[str] = []

        # ── Batch cancel ──────────────────────────────────────────────────────
        if cancels:
            cancel_ids = [c.order_id for c in cancels]
            if self.settings.DRY_RUN:
                log.info("DRY_RUN cancel: %s", cancel_ids)
                cancelled_ids.extend(cancel_ids)
            else:
                # Determine confirm-cancel mode from the first cancel's token
                first_token = cancels[0].token_id
                confirm_mode = self._tracker(first_token).confirm_cancel_mode

                if confirm_mode:
                    await clob_client.cancel_orders(cancel_ids)
                    cancelled_ids.extend(cancel_ids)
                else:
                    # Fire-and-forget: dispatch cancel and immediately proceed to place
                    asyncio.ensure_future(clob_client.cancel_orders(cancel_ids))
                    cancelled_ids.extend(cancel_ids)

        # ── Batch place ───────────────────────────────────────────────────────
        for pm in places:
            if self.settings.DRY_RUN:
                log.info(
                    "DRY_RUN place: %s %s@%.4f size=%s strategy=%s",
                    pm.intent.token_id, pm.intent.side, pm.intent.price,
                    pm.intent.size, pm.intent.strategy,
                )
                placed_ids.append(f"dry_run_{pm.intent.token_id}_{pm.intent.side}")
            else:
                order_id = await self._place_with_retry(pm.intent, clob_client)
                if order_id:
                    placed_ids.append(order_id)

        return {"cancelled": cancelled_ids, "placed": placed_ids}
