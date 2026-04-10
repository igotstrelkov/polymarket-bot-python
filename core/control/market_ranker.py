"""
Market Ranker — EV-based market selection and capital allocation.

PRD §4.4.2 EV model:
    maker_EV = spread_EV + reward_EV + rebate_EV
               - adverse_selection_cost - inventory_cost - event_risk_cost

Cold-start behavior (< 100 fills OR < 24h live data per market):
  - fill_probability: linear 0.5 at mid → 0.05 at 5 ticks out
  - adverse_selection_cost: fixed 1 tick (conservative overestimate)
  - ranking falls back primarily to spread width, rewardsDailyRate, depth

Capital allocation:
  - Select top N markets by maker_EV, bounded by MM_MAX_MARKETS
  - Exclude EV ≤ 0 regardless of rank
  - Each selected market gets ≥ MM_MIN_ORDER_SIZE per side
  - Remaining budget allocated pro-rata to EV score
  - Subject to MAX_PER_MARKET and MAX_TOTAL_EXPOSURE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config.settings import Settings

log = logging.getLogger(__name__)

_COLD_START_FILL_THRESHOLD = 100
_COLD_START_HOURS_THRESHOLD = 24.0
_EVENT_RISK_WINDOW_H = 6.0   # markets within 6h of resolution incur event risk cost


@dataclass
class MarketEVInputs:
    """Per-market inputs to the EV model.

    When fill_count < 100 OR hours_live < 24, the ranker applies cold-start
    fallbacks for fill_probability and adverse_selection_cost.
    """
    token_id: str
    tick_size: float

    # Book / spread
    observed_half_spread: float          # distance from mid to our best quote
    posted_ticks_from_mid: float = 1.0  # distance at which we intend to post

    # Fill history (used to determine cold-start)
    fill_count: int = 0
    hours_live: float = 0.0

    # Data-driven inputs (None → cold-start fallback will be used)
    fill_probability: float | None = None     # historical fill rate at posted_ticks_from_mid
    adverse_selection_cost: float | None = None  # avg 30s post-fill markout

    # Reward / rebate
    rewards_daily_rate: float | None = None
    proximity_score: float = 0.0              # estimated share of reward pool [0, 1]

    # Rebate
    rebate_rate_decimal: float = 0.0          # fee_rate_bps / 10_000
    expected_daily_volume: float = 0.0

    # Inventory
    inventory_skew: float = 0.0              # value-weighted skew in [-1, 1]

    # Resolution timing
    time_to_resolution_h: float | None = None


@dataclass
class RankedMarket:
    """Output from the ranker for a single market."""
    token_id: str
    maker_ev: float

    # EV term breakdown (for observability / tuning)
    spread_ev: float
    reward_ev: float
    rebate_ev: float
    adverse_selection_cost: float
    inventory_cost: float
    event_risk_cost: float
    is_cold_start: bool

    # Set during capital allocation
    allocated_exposure: float = 0.0


def _cold_start_fill_prob(ticks_from_mid: float) -> float:
    """Linear fill-probability curve for cold-start markets.

    0.5 at mid (0 ticks) → 0.05 at 5 ticks out, clamped below at 0.05.
    """
    slope = (0.5 - 0.05) / 5.0  # 0.09 per tick
    return max(0.05, 0.5 - slope * ticks_from_mid)


def _compute_ev(inputs: MarketEVInputs) -> RankedMarket:
    """Compute all EV terms for a single market."""
    is_cold = (
        inputs.fill_count < _COLD_START_FILL_THRESHOLD
        or inputs.hours_live < _COLD_START_HOURS_THRESHOLD
    )

    # ── Fill probability ──────────────────────────────────────────────────────
    if is_cold or inputs.fill_probability is None:
        fill_prob = _cold_start_fill_prob(inputs.posted_ticks_from_mid)
    else:
        fill_prob = inputs.fill_probability

    # ── spread_EV ─────────────────────────────────────────────────────────────
    spread_ev = inputs.observed_half_spread * fill_prob

    # ── reward_EV ─────────────────────────────────────────────────────────────
    reward_ev = 0.0
    if inputs.rewards_daily_rate is not None:
        reward_ev = inputs.rewards_daily_rate * inputs.proximity_score

    # ── rebate_EV ─────────────────────────────────────────────────────────────
    rebate_ev = inputs.rebate_rate_decimal * inputs.expected_daily_volume

    # ── adverse_selection_cost ────────────────────────────────────────────────
    if is_cold or inputs.adverse_selection_cost is None:
        adverse_selection_cost = inputs.tick_size  # conservative: 1 tick
    else:
        adverse_selection_cost = inputs.adverse_selection_cost

    # ── inventory_cost ────────────────────────────────────────────────────────
    inventory_cost = abs(inputs.inventory_skew) * inputs.tick_size

    # ── event_risk_cost ───────────────────────────────────────────────────────
    event_risk_cost = 0.0
    if (
        inputs.time_to_resolution_h is not None
        and 0 < inputs.time_to_resolution_h < _EVENT_RISK_WINDOW_H
    ):
        # Linear decay: 0 at 6h, scales to observed_half_spread at 0h
        fraction = 1.0 - inputs.time_to_resolution_h / _EVENT_RISK_WINDOW_H
        event_risk_cost = fraction * inputs.observed_half_spread

    maker_ev = (
        spread_ev
        + reward_ev
        + rebate_ev
        - adverse_selection_cost
        - inventory_cost
        - event_risk_cost
    )

    return RankedMarket(
        token_id=inputs.token_id,
        maker_ev=maker_ev,
        spread_ev=spread_ev,
        reward_ev=reward_ev,
        rebate_ev=rebate_ev,
        adverse_selection_cost=adverse_selection_cost,
        inventory_cost=inventory_cost,
        event_risk_cost=event_risk_cost,
        is_cold_start=is_cold,
    )


def rank(
    markets: list[MarketEVInputs],
    settings: Settings,
) -> list[RankedMarket]:
    """Rank markets by maker_EV and allocate capital.

    Returns selected markets (EV > 0, top MM_MAX_MARKETS) with
    allocated_exposure populated.  Markets are sorted by maker_EV descending.
    """
    # ── Compute EV for every candidate ───────────────────────────────────────
    ranked: list[RankedMarket] = [_compute_ev(m) for m in markets]

    # ── Filter EV ≤ 0 ────────────────────────────────────────────────────────
    positive = [r for r in ranked if r.maker_ev > 0]

    # ── Sort by EV descending and cap at MM_MAX_MARKETS ───────────────────────
    positive.sort(key=lambda r: r.maker_ev, reverse=True)
    selected = positive[: settings.MM_MAX_MARKETS]

    if not selected:
        log.debug("MarketRanker: no markets with EV > 0")
        return []

    # ── Capital allocation ────────────────────────────────────────────────────
    total_exposure = settings.MAX_TOTAL_EXPOSURE
    max_per_market = settings.MAX_PER_MARKET
    min_size = float(settings.MM_MIN_ORDER_SIZE)

    # Each market gets the minimum floor first
    floor_total = min_size * len(selected)
    remaining = max(0.0, total_exposure - floor_total)

    total_ev = sum(r.maker_ev for r in selected)

    for r in selected:
        if total_ev > 0:
            pro_rata = (r.maker_ev / total_ev) * remaining
        else:
            pro_rata = remaining / len(selected)
        allocation = min(max_per_market, min_size + pro_rata)
        r.allocated_exposure = round(allocation, 6)

    log.info(
        "MarketRanker: selected %d markets (of %d candidates), "
        "total_ev=%.4f",
        len(selected),
        len(markets),
        total_ev,
    )
    return selected
