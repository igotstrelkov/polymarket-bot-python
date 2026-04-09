"""
Unit tests for core/execution/risk_gate.py.

Covers all pre-trade hard checks: kill switch, session health, daily loss,
drawdown, total exposure, per-market exposure, inventory halt, and accepting_orders
defense-in-depth (FR-309).
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from core.control.capability_enricher import MarketCapabilityModel
from core.execution.risk_gate import RiskState, check, filter_intents
from core.execution.types import OrderIntent


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    base = dict(
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="pass",
        MAX_TOTAL_EXPOSURE=2000.0,
        MAX_PER_MARKET=100.0,
        MAX_DAILY_LOSS=500.0,
        MAX_DRAWDOWN=500.0,
        CANCEL_CONFIRM_THRESHOLD_PCT=5.0,
    )
    base.update(overrides)
    return Settings(**base)


def make_intent(
    *,
    token_id: str = "tok1",
    side: str = "BUY",
    price: float = 0.50,
    size: float = 10.0,
    strategy: str = "A",
) -> OrderIntent:
    return OrderIntent(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        time_in_force="GTC",
        post_only=True,
        expiration=None,
        strategy=strategy,
        fee_rate_bps=78,
        neg_risk=False,
        tick_size=0.01,
    )


def make_market(*, accepting_orders: bool = True) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id="tok1",
        condition_id="cond1",
        tick_size=0.01,
        minimum_order_size=1.0,
        neg_risk=False,
        fees_enabled=True,
        fee_rate_bps=78,
        seconds_delay=0,
        accepting_orders=accepting_orders,
        game_start_time=None,
        resolution_time=None,
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )


def make_state(**overrides) -> RiskState:
    s = RiskState()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ── Kill switch ───────────────────────────────────────────────────────────────

def test_fails_when_kill_switch_active():
    state = make_state(kill_switch_active=True)
    result = check(make_intent(), make_market(), state, make_settings())
    assert result.passed is False
    assert "kill_switch" in result.reason


def test_passes_when_kill_switch_inactive():
    state = make_state(kill_switch_active=False)
    result = check(make_intent(), make_market(), state, make_settings())
    assert result.passed is True


# ── Session health ────────────────────────────────────────────────────────────

def test_fails_when_session_unhealthy():
    state = make_state(session_healthy=False)
    result = check(make_intent(), make_market(), state, make_settings())
    assert result.passed is False
    assert "session" in result.reason


def test_passes_when_session_healthy():
    state = make_state(session_healthy=True)
    result = check(make_intent(), make_market(), state, make_settings())
    assert result.passed is True


# ── Daily loss limit (FR-303) ─────────────────────────────────────────────────

def test_fails_at_daily_loss_limit():
    settings = make_settings(MAX_DAILY_LOSS=500.0)
    state = make_state(daily_loss=500.0)
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is False
    assert "daily_loss" in result.reason


def test_fails_above_daily_loss_limit():
    settings = make_settings(MAX_DAILY_LOSS=500.0)
    state = make_state(daily_loss=501.0)
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is False


def test_passes_below_daily_loss_limit():
    settings = make_settings(MAX_DAILY_LOSS=500.0)
    state = make_state(daily_loss=499.0)
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is True


# ── Drawdown limit (FR-304) ───────────────────────────────────────────────────

def test_fails_at_drawdown_limit():
    settings = make_settings(MAX_DRAWDOWN=500.0)
    state = make_state(drawdown=500.0)
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is False
    assert "drawdown" in result.reason


def test_passes_below_drawdown_limit():
    settings = make_settings(MAX_DRAWDOWN=500.0)
    state = make_state(drawdown=499.9)
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is True


# ── Total portfolio exposure (FR-301) ─────────────────────────────────────────

def test_fails_when_total_exposure_would_be_exceeded():
    """current=1990, order notional=0.50×10=5 → 1995 ≤ 2000 → passes."""
    settings = make_settings(MAX_TOTAL_EXPOSURE=2000.0)
    state = make_state(total_exposure=1990.0)
    # intent: price=0.50 × size=10 = 5.0 notional
    result = check(make_intent(price=0.50, size=10), make_market(), state, settings)
    assert result.passed is True


def test_fails_when_total_exposure_exceeded():
    """current=1998, order notional=0.50×10=5 → 2003 > 2000 → fails."""
    settings = make_settings(MAX_TOTAL_EXPOSURE=2000.0)
    state = make_state(total_exposure=1998.0)
    result = check(make_intent(price=0.50, size=10), make_market(), state, settings)
    assert result.passed is False
    assert "total_exposure" in result.reason


def test_passes_with_zero_existing_exposure():
    settings = make_settings(MAX_TOTAL_EXPOSURE=2000.0)
    state = make_state(total_exposure=0.0)
    result = check(make_intent(price=0.50, size=10), make_market(), state, settings)
    assert result.passed is True


# ── Per-market exposure (FR-302) ──────────────────────────────────────────────

def test_fails_when_per_market_exposure_exceeded():
    """market exposure=96, order notional=5 → 101 > 100 → fails."""
    settings = make_settings(MAX_PER_MARKET=100.0)
    state = make_state(per_market_exposure={"tok1": 96.0})
    result = check(make_intent(token_id="tok1", price=0.50, size=10), make_market(), state, settings)
    assert result.passed is False
    assert "per_market_exposure" in result.reason


def test_passes_when_per_market_exposure_within_limit():
    settings = make_settings(MAX_PER_MARKET=100.0)
    state = make_state(per_market_exposure={"tok1": 90.0})
    result = check(make_intent(token_id="tok1", price=0.50, size=10), make_market(), state, settings)
    assert result.passed is True


def test_per_market_exposure_zero_for_unknown_token():
    """Unknown token starts at 0 exposure."""
    settings = make_settings(MAX_PER_MARKET=100.0)
    state = make_state(per_market_exposure={})
    result = check(make_intent(token_id="tok_new", price=0.50, size=10), make_market(), state, settings)
    assert result.passed is True


# ── Inventory halt (FR-306) ───────────────────────────────────────────────────

def test_fails_when_token_in_inventory_halted():
    state = make_state(inventory_halted={"tok1"})
    result = check(make_intent(token_id="tok1"), make_market(), state, make_settings())
    assert result.passed is False
    assert "inventory_halted" in result.reason


def test_passes_when_token_not_in_inventory_halted():
    state = make_state(inventory_halted={"tok_other"})
    result = check(make_intent(token_id="tok1"), make_market(), state, make_settings())
    assert result.passed is True


def test_passes_when_inventory_halted_set_is_empty():
    state = make_state(inventory_halted=set())
    result = check(make_intent(), make_market(), state, make_settings())
    assert result.passed is True


# ── accepting_orders defense-in-depth (FR-309) ────────────────────────────────

def test_fails_when_accepting_orders_false():
    """Defense-in-depth: accepting_orders=False should have been caught upstream."""
    result = check(make_intent(), make_market(accepting_orders=False), make_state(), make_settings())
    assert result.passed is False
    assert "accepting_orders" in result.reason


def test_passes_when_accepting_orders_true():
    result = check(make_intent(), make_market(accepting_orders=True), make_state(), make_settings())
    assert result.passed is True


# ── Happy path ────────────────────────────────────────────────────────────────

def test_passes_when_all_checks_clear():
    settings = make_settings(
        MAX_TOTAL_EXPOSURE=2000.0,
        MAX_PER_MARKET=100.0,
        MAX_DAILY_LOSS=500.0,
        MAX_DRAWDOWN=500.0,
    )
    state = RiskState(
        total_exposure=0.0,
        per_market_exposure={},
        inventory_halted=set(),
        kill_switch_active=False,
        session_healthy=True,
        daily_loss=0.0,
        drawdown=0.0,
    )
    result = check(make_intent(), make_market(), state, settings)
    assert result.passed is True
    assert result.reason == ""


# ── filter_intents ────────────────────────────────────────────────────────────

def test_filter_intents_returns_only_passing():
    settings = make_settings(MAX_TOTAL_EXPOSURE=2000.0, MAX_PER_MARKET=100.0)
    # tok1 passes; tok2 is inventory-halted
    state = make_state(inventory_halted={"tok2"})
    intents = [
        make_intent(token_id="tok1"),
        make_intent(token_id="tok2"),
    ]
    markets_by_token = {
        "tok1": make_market(),
        "tok2": MarketCapabilityModel(
            token_id="tok2", condition_id="cond2", tick_size=0.01, minimum_order_size=1.0,
            neg_risk=False, fees_enabled=True, fee_rate_bps=0, seconds_delay=0,
            accepting_orders=True, game_start_time=None, resolution_time=None,
            rewards_min_size=None, rewards_max_spread=None, rewards_daily_rate=None,
            adjusted_midpoint=None, tags=[],
        ),
    }
    # filter_intents uses a single market; call per-token
    passed_tok1 = filter_intents([intents[0]], markets_by_token["tok1"], state, settings)
    passed_tok2 = filter_intents([intents[1]], markets_by_token["tok2"], state, settings)
    assert len(passed_tok1) == 1
    assert len(passed_tok2) == 0


def test_filter_intents_returns_all_when_all_pass():
    settings = make_settings()
    state = make_state()
    intents = [make_intent(token_id="tok1"), make_intent(token_id="tok1", side="SELL")]
    result = filter_intents(intents, make_market(), state, settings)
    assert len(result) == 2


def test_filter_intents_returns_empty_when_kill_switch():
    state = make_state(kill_switch_active=True)
    intents = [make_intent()]
    result = filter_intents(intents, make_market(), state, make_settings())
    assert result == []
