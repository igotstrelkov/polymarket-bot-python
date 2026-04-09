"""
Capability Enricher — the ONLY module that maps raw Gamma API fields to the
internal snake_case model. No other module may reference raw API field names.

PRD Design Principle P2 field naming:
  Gamma API:     camelCase  (acceptingOrders, secondsDelay, gameStartTime,
                             negRisk, tickSize, minimumOrderSize, resolutionTime,
                             feesEnabled, rewardsMinSize, rewardsMaxSpread,
                             adjustedMidpoint)
  Internal:      snake_case throughout

FR-102: Extract all per-market capabilities into MarketCapabilityModel.
FR-103a: detect_mutations() compares two snapshots; called by UniverseScanner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)


class MutationType(Enum):
    RESOLUTION_TIME_CHANGED = auto()
    ACCEPTING_ORDERS_FLIPPED_FALSE = auto()
    FEE_RATE_CHANGED = auto()
    SECONDS_DELAY_BECAME_NONZERO = auto()


@dataclass
class MarketCapabilityModel:
    """Unified internal representation of a market's capabilities.

    No prev_* fields here — this represents current state only.
    UniverseScanner (Step 8) owns the snapshot dict and comparison logic.
    """
    token_id: str
    condition_id: str
    tick_size: float
    minimum_order_size: float
    neg_risk: bool
    fees_enabled: bool          # feesEnabled — authoritative eligibility switch (FR-451)
    fee_rate_bps: int           # from /fee-rate/{token_id}, field 'base_fee'
    seconds_delay: int          # from Gamma secondsDelay
    accepting_orders: bool      # from Gamma acceptingOrders
    game_start_time: datetime | None
    resolution_time: datetime | None
    rewards_min_size: float | None
    rewards_max_spread: float | None
    rewards_daily_rate: float | None
    adjusted_midpoint: float | None
    tags: list[str]


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 string or Unix timestamp to a timezone-aware datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        log.warning("Could not parse datetime: %r", value)
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def enrich(raw_market: dict, fee_rate_bps: int = 0) -> MarketCapabilityModel:
    """Map Gamma camelCase API response → internal snake_case MarketCapabilityModel.

    `fee_rate_bps` is passed in from the fee cache / /fee-rate/{token_id} call
    (response field 'base_fee') — it is not present in the raw Gamma market dict.

    The `clob_token_ids` list in Gamma contains one entry per outcome. The caller
    is responsible for iterating over tokens; this function enriches one token at a time.
    Pass `token_id` as a separate argument via the caller — it is not extracted here
    since Gamma nests token IDs inside `clobTokenIds`.
    """
    # Token / condition IDs — callers normalise multi-outcome markets
    token_id = raw_market.get("token_id", "")
    condition_id = raw_market.get("conditionId") or raw_market.get("condition_id", "")

    # Numeric market parameters (Gamma camelCase → snake_case)
    tick_size = float(raw_market.get("tickSize") or raw_market.get("tick_size") or 0.01)
    minimum_order_size = float(
        raw_market.get("minimumOrderSize") or raw_market.get("minimum_order_size") or 0.0
    )
    seconds_delay = int(raw_market.get("secondsDelay") or raw_market.get("seconds_delay") or 0)

    # Boolean flags
    neg_risk = bool(raw_market.get("negRisk") or raw_market.get("neg_risk") or False)
    fees_enabled = bool(raw_market.get("feesEnabled") or False)
    accepting_orders = bool(raw_market.get("acceptingOrders") or False)

    # Datetime fields
    game_start_time = _parse_datetime(
        raw_market.get("gameStartTime") or raw_market.get("game_start_time")
    )
    resolution_time = _parse_datetime(
        raw_market.get("resolutionTime") or raw_market.get("resolution_time")
    )

    # Reward parameters — sourced from dedicated rewards endpoints (FR-157);
    # Gamma fields used as fallback only.
    rewards_min_size = _opt_float(
        raw_market.get("rewardsMinSize") or raw_market.get("rewards_min_size")
    )
    rewards_max_spread = _opt_float(
        raw_market.get("rewardsMaxSpread") or raw_market.get("rewards_max_spread")
    )
    rewards_daily_rate = _opt_float(
        raw_market.get("rewardsDailyRate") or raw_market.get("rewards_daily_rate")
    )
    adjusted_midpoint = _opt_float(
        raw_market.get("adjustedMidpoint") or raw_market.get("adjusted_midpoint")
    )

    tags = raw_market.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return MarketCapabilityModel(
        token_id=token_id,
        condition_id=condition_id,
        tick_size=tick_size,
        minimum_order_size=minimum_order_size,
        neg_risk=neg_risk,
        fees_enabled=fees_enabled,
        fee_rate_bps=fee_rate_bps,
        seconds_delay=seconds_delay,
        accepting_orders=accepting_orders,
        game_start_time=game_start_time,
        resolution_time=resolution_time,
        rewards_min_size=rewards_min_size,
        rewards_max_spread=rewards_max_spread,
        rewards_daily_rate=rewards_daily_rate,
        adjusted_midpoint=adjusted_midpoint,
        tags=list(tags),
    )


def detect_mutations(
    old: MarketCapabilityModel,
    new: MarketCapabilityModel,
) -> list[MutationType]:
    """FR-103a: compare two MarketCapabilityModel snapshots and return changed fields.

    Called by UniverseScanner, which holds the previous snapshot dict keyed by
    condition_id. The caller passes in both snapshots; this function is stateless.
    """
    mutations: list[MutationType] = []

    if old.resolution_time != new.resolution_time:
        mutations.append(MutationType.RESOLUTION_TIME_CHANGED)

    if old.accepting_orders is True and new.accepting_orders is False:
        mutations.append(MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE)

    if old.fee_rate_bps != new.fee_rate_bps:
        mutations.append(MutationType.FEE_RATE_CHANGED)

    if old.seconds_delay == 0 and new.seconds_delay != 0:
        mutations.append(MutationType.SECONDS_DELAY_BECAME_NONZERO)

    return mutations
