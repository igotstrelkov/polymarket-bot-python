"""
Unit tests for core/control/capability_enricher.py.

Verifies:
- camelCase Gamma fields → snake_case internal model
- All four FR-103a mutation types
- MarketCapabilityModel has no prev_* fields
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from core.control.capability_enricher import (
    MarketCapabilityModel,
    MutationType,
    detect_mutations,
    enrich,
)


def _base_raw() -> dict:
    """Minimal valid Gamma API response dict using camelCase field names."""
    return {
        "token_id": "tok1",
        "conditionId": "cond1",
        "tickSize": "0.01",
        "minimumOrderSize": "5.0",
        "negRisk": False,
        "feesEnabled": True,
        "secondsDelay": 0,
        "acceptingOrders": True,
        "gameStartTime": None,
        "resolutionTime": "2026-12-31T00:00:00Z",
        "rewardsMinSize": "10.0",
        "rewardsMaxSpread": "0.05",
        "rewardsDailyRate": "0.001",
        "adjustedMidpoint": "0.52",
        "tags": ["crypto"],
    }


def _base_model(**overrides) -> MarketCapabilityModel:
    raw = _base_raw()
    raw.update(overrides)
    return enrich(raw, fee_rate_bps=overrides.get("_fee_rate_bps", 78))


# ── Field mapping: camelCase → snake_case ────────────────────────────────────

def test_accepting_orders_mapped():
    m = enrich(_base_raw(), fee_rate_bps=0)
    assert m.accepting_orders is True


def test_seconds_delay_mapped():
    raw = _base_raw()
    raw["secondsDelay"] = 3
    m = enrich(raw, fee_rate_bps=0)
    assert m.seconds_delay == 3


def test_game_start_time_mapped_from_iso():
    raw = _base_raw()
    raw["gameStartTime"] = "2026-06-01T15:00:00Z"
    m = enrich(raw, fee_rate_bps=0)
    assert isinstance(m.game_start_time, datetime)
    assert m.game_start_time.tzinfo is not None


def test_neg_risk_mapped():
    raw = _base_raw()
    raw["negRisk"] = True
    m = enrich(raw, fee_rate_bps=0)
    assert m.neg_risk is True


def test_tick_size_mapped():
    raw = _base_raw()
    raw["tickSize"] = "0.05"
    m = enrich(raw, fee_rate_bps=0)
    assert m.tick_size == pytest.approx(0.05)


def test_fees_enabled_mapped():
    raw = _base_raw()
    raw["feesEnabled"] = True
    m = enrich(raw, fee_rate_bps=0)
    assert m.fees_enabled is True


def test_fees_enabled_false_when_absent():
    raw = _base_raw()
    raw.pop("feesEnabled")
    m = enrich(raw, fee_rate_bps=0)
    assert m.fees_enabled is False


def test_fee_rate_bps_comes_from_argument():
    """fee_rate_bps is NOT in the Gamma response — it comes from /fee-rate/{token_id}."""
    m = enrich(_base_raw(), fee_rate_bps=78)
    assert m.fee_rate_bps == 78


def test_resolution_time_parsed():
    m = enrich(_base_raw(), fee_rate_bps=0)
    assert isinstance(m.resolution_time, datetime)
    assert m.resolution_time == datetime(2026, 12, 31, 0, 0, 0, tzinfo=timezone.utc)


def test_tags_mapped_as_list():
    m = enrich(_base_raw(), fee_rate_bps=0)
    assert m.tags == ["crypto"]


def test_rewards_fields_mapped():
    m = enrich(_base_raw(), fee_rate_bps=0)
    assert m.rewards_min_size == pytest.approx(10.0)
    assert m.rewards_max_spread == pytest.approx(0.05)
    assert m.adjusted_midpoint == pytest.approx(0.52)


def test_game_start_time_none_when_absent():
    raw = _base_raw()
    raw["gameStartTime"] = None
    m = enrich(raw, fee_rate_bps=0)
    assert m.game_start_time is None


# ── No prev_* fields ──────────────────────────────────────────────────────────

def test_model_has_no_prev_fields():
    """MarketCapabilityModel must not contain any prev_* fields (per spec comment)."""
    field_names = {f.name for f in dataclasses.fields(MarketCapabilityModel)}
    prev_fields = {n for n in field_names if n.startswith("prev_")}
    assert prev_fields == set(), f"Unexpected prev_* fields: {prev_fields}"


# ── detect_mutations() — all four FR-103a types ───────────────────────────────

def _make_model(**kwargs) -> MarketCapabilityModel:
    """Build a MarketCapabilityModel with sensible defaults, overridable per field."""
    defaults = dict(
        token_id="tok1",
        condition_id="cond1",
        tick_size=0.01,
        minimum_order_size=5.0,
        neg_risk=False,
        fees_enabled=True,
        fee_rate_bps=78,
        seconds_delay=0,
        accepting_orders=True,
        game_start_time=None,
        resolution_time=datetime(2026, 12, 31, tzinfo=timezone.utc),
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )
    defaults.update(kwargs)
    return MarketCapabilityModel(**defaults)


def test_mutation_resolution_time_changed():
    old = _make_model(resolution_time=datetime(2026, 12, 31, tzinfo=timezone.utc))
    new = _make_model(resolution_time=datetime(2026, 12, 30, tzinfo=timezone.utc))
    assert MutationType.RESOLUTION_TIME_CHANGED in detect_mutations(old, new)


def test_mutation_accepting_orders_flipped_false():
    old = _make_model(accepting_orders=True)
    new = _make_model(accepting_orders=False)
    assert MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE in detect_mutations(old, new)


def test_mutation_accepting_orders_not_triggered_on_true_to_true():
    old = _make_model(accepting_orders=True)
    new = _make_model(accepting_orders=True)
    assert MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE not in detect_mutations(old, new)


def test_mutation_accepting_orders_not_triggered_false_to_true():
    """Going from False → True is a recovery, not this mutation type."""
    old = _make_model(accepting_orders=False)
    new = _make_model(accepting_orders=True)
    assert MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE not in detect_mutations(old, new)


def test_mutation_fee_rate_changed():
    old = _make_model(fee_rate_bps=78)
    new = _make_model(fee_rate_bps=100)
    assert MutationType.FEE_RATE_CHANGED in detect_mutations(old, new)


def test_mutation_fee_rate_not_triggered_when_same():
    old = _make_model(fee_rate_bps=78)
    new = _make_model(fee_rate_bps=78)
    assert MutationType.FEE_RATE_CHANGED not in detect_mutations(old, new)


def test_mutation_seconds_delay_became_nonzero():
    old = _make_model(seconds_delay=0)
    new = _make_model(seconds_delay=3)
    assert MutationType.SECONDS_DELAY_BECAME_NONZERO in detect_mutations(old, new)


def test_mutation_seconds_delay_not_triggered_nonzero_to_nonzero():
    old = _make_model(seconds_delay=2)
    new = _make_model(seconds_delay=5)
    assert MutationType.SECONDS_DELAY_BECAME_NONZERO not in detect_mutations(old, new)


def test_no_mutations_on_identical_models():
    m = _make_model()
    assert detect_mutations(m, m) == []


def test_multiple_mutations_detected_simultaneously():
    old = _make_model(fee_rate_bps=78, accepting_orders=True)
    new = _make_model(fee_rate_bps=100, accepting_orders=False)
    mutations = detect_mutations(old, new)
    assert MutationType.FEE_RATE_CHANGED in mutations
    assert MutationType.ACCEPTING_ORDERS_FLIPPED_FALSE in mutations
