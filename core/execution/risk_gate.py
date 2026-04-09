"""
Risk Gate — synchronous hard checks before any order placement (FR-300, FR-305).

All checks are synchronous and must complete before any order leaves the process.
The gate is defense-in-depth: upstream components (strategy gates, accepting_orders
check in UniverseScanner) should have already filtered invalid orders. The Risk Gate
is the last enforcer.

FR-305: Pre-trade risk validation before every order submission.
FR-309: Defense-in-depth accepting_orders check (authoritative: FR-116).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config.settings import Settings
from core.control.capability_enricher import MarketCapabilityModel
from core.execution.types import OrderIntent

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Mutable snapshot of current portfolio risk state.

    Updated by the Order Ledger and Fill & Position Ledger after each fill/cancel.
    Callers are responsible for keeping this consistent with confirmed positions.
    """
    total_exposure: float = 0.0
    per_market_exposure: dict[str, float] = field(default_factory=dict)
    inventory_halted: set[str] = field(default_factory=set)   # token_ids halted
    kill_switch_active: bool = False
    session_healthy: bool = True
    daily_loss: float = 0.0     # FR-303: positive = loss amount
    drawdown: float = 0.0       # FR-304: positive = drawdown from peak equity


@dataclass
class RiskCheckResult:
    passed: bool
    reason: str = ""


def check(
    intent: OrderIntent,
    market: MarketCapabilityModel,
    state: RiskState,
    settings: Settings,
) -> RiskCheckResult:
    """Run all pre-trade risk checks for a single OrderIntent.

    Returns RiskCheckResult(passed=False, reason=...) on the first failing check.
    Returns RiskCheckResult(passed=True) when all checks pass.

    Checks are ordered from cheapest/most-common-failure to most expensive:
    1. Global kill switch
    2. Session health
    3. Daily loss limit (FR-303)
    4. Drawdown limit (FR-304)
    5. Total portfolio exposure (FR-301)
    6. Per-market exposure (FR-302)
    7. Inventory halt (FR-306)
    8. accepting_orders — defense-in-depth (FR-309)
    """
    # 1. Global kill switch (FR-211)
    if state.kill_switch_active:
        return RiskCheckResult(passed=False, reason="kill_switch_active")

    # 2. Session health — do not place into a potentially disconnected session
    if not state.session_healthy:
        return RiskCheckResult(passed=False, reason="session_unhealthy")

    # 3. Daily loss limit (FR-303)
    if state.daily_loss >= settings.MAX_DAILY_LOSS:
        return RiskCheckResult(
            passed=False,
            reason=f"daily_loss_limit: {state.daily_loss:.2f} >= {settings.MAX_DAILY_LOSS}",
        )

    # 4. Drawdown limit (FR-304)
    if state.drawdown >= settings.MAX_DRAWDOWN:
        return RiskCheckResult(
            passed=False,
            reason=f"drawdown_limit: {state.drawdown:.2f} >= {settings.MAX_DRAWDOWN}",
        )

    # 5. Total portfolio exposure (FR-301)
    order_notional = intent.price * intent.size
    if state.total_exposure + order_notional > settings.MAX_TOTAL_EXPOSURE:
        return RiskCheckResult(
            passed=False,
            reason=(
                f"total_exposure: {state.total_exposure:.2f} + {order_notional:.2f} "
                f"> {settings.MAX_TOTAL_EXPOSURE}"
            ),
        )

    # 6. Per-market exposure (FR-302)
    market_exposure = state.per_market_exposure.get(intent.token_id, 0.0)
    if market_exposure + order_notional > settings.MAX_PER_MARKET:
        return RiskCheckResult(
            passed=False,
            reason=(
                f"per_market_exposure [{intent.token_id}]: "
                f"{market_exposure:.2f} + {order_notional:.2f} > {settings.MAX_PER_MARKET}"
            ),
        )

    # 7. Inventory halt (FR-306)
    if intent.token_id in state.inventory_halted:
        return RiskCheckResult(
            passed=False,
            reason=f"inventory_halted: {intent.token_id}",
        )

    # 8. accepting_orders — defense-in-depth (FR-309)
    if not market.accepting_orders:
        log.warning(
            "RiskGate: accepting_orders=False reached the gate for %s — "
            "upstream filter missed this (data-layer validation failure)",
            intent.token_id,
        )
        return RiskCheckResult(
            passed=False,
            reason=f"accepting_orders=False [{intent.token_id}] (data-layer validation failure)",
        )

    return RiskCheckResult(passed=True)


def filter_intents(
    intents: list[OrderIntent],
    market: MarketCapabilityModel,
    state: RiskState,
    settings: Settings,
) -> list[OrderIntent]:
    """Return only the intents that pass all risk checks.

    Logs rejected intents at WARNING level.
    """
    passed: list[OrderIntent] = []
    for intent in intents:
        result = check(intent, market, state, settings)
        if result.passed:
            passed.append(intent)
        else:
            log.warning(
                "RiskGate rejected intent: token=%s side=%s price=%.4f size=%s "
                "strategy=%s reason=%s",
                intent.token_id, intent.side, intent.price, intent.size,
                intent.strategy, result.reason,
            )
    return passed
