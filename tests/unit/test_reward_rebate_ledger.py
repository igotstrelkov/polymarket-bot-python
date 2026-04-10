"""
Unit tests for core/ledger/reward_rebate_ledger.py.

Covers: reward snapshots, unscored token detection, maker ratio,
rebate recording, daily totals, and FR-506 double-redemption prevention.
"""

from __future__ import annotations

import pytest

from core.ledger.reward_rebate_ledger import RewardAndRebateLedger


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_ledger() -> RewardAndRebateLedger:
    return RewardAndRebateLedger()


def record_snapshot(
    ledger: RewardAndRebateLedger,
    token_id: str = "tok1",
    *,
    is_scored: bool = True,
    daily_rate: float | None = 10.0,
    rewards_percentage: float | None = 0.05,
    accumulated_today: float = 0.0,
):
    return ledger.record_reward_snapshot(
        token_id=token_id,
        rewards_percentage=rewards_percentage,
        is_scored=is_scored,
        daily_rate=daily_rate,
        accumulated_today=accumulated_today,
    )


def record_rebate(
    ledger: RewardAndRebateLedger,
    fill_id: str,
    *,
    strategy: str = "A",
    is_maker: bool = True,
    rebate_amount: float = 0.10,
):
    return ledger.record_rebate(
        fill_id=fill_id,
        token_id="tok1",
        strategy=strategy,
        is_maker=is_maker,
        rebate_amount=rebate_amount,
    )


# ── Reward snapshots ──────────────────────────────────────────────────────────

def test_record_snapshot_stores_latest():
    ledger = make_ledger()
    snap = record_snapshot(ledger, "tok1", is_scored=True)
    assert ledger.get_reward_snapshot("tok1") is snap


def test_snapshot_overwrites_previous():
    ledger = make_ledger()
    record_snapshot(ledger, "tok1", is_scored=True)
    record_snapshot(ledger, "tok1", is_scored=False)
    snap = ledger.get_reward_snapshot("tok1")
    assert snap.is_scored is False


def test_snapshot_none_for_unknown_token():
    ledger = make_ledger()
    assert ledger.get_reward_snapshot("unknown") is None


# ── Unscored detection (FR-158) ───────────────────────────────────────────────

def test_unscored_tokens_empty_when_all_scored():
    ledger = make_ledger()
    record_snapshot(ledger, "tok1", is_scored=True)
    record_snapshot(ledger, "tok2", is_scored=True)
    assert ledger.unscored_tokens() == []


def test_unscored_tokens_returns_unscored():
    ledger = make_ledger()
    record_snapshot(ledger, "tok1", is_scored=True)
    record_snapshot(ledger, "tok2", is_scored=False)
    assert ledger.unscored_tokens() == ["tok2"]


# ── Maker ratio (FR-453) ──────────────────────────────────────────────────────

def test_maker_ratio_1_when_no_fills():
    ledger = make_ledger()
    assert ledger.maker_ratio() == pytest.approx(1.0)


def test_maker_ratio_all_maker():
    ledger = make_ledger()
    for i in range(5):
        record_rebate(ledger, f"f{i}", is_maker=True)
    assert ledger.maker_ratio() == pytest.approx(1.0)


def test_maker_ratio_mixed():
    ledger = make_ledger()
    record_rebate(ledger, "f1", is_maker=True)
    record_rebate(ledger, "f2", is_maker=True)
    record_rebate(ledger, "f3", is_maker=False)
    record_rebate(ledger, "f4", is_maker=False)
    assert ledger.maker_ratio() == pytest.approx(0.5)


def test_maker_ratio_excludes_strategy_b():
    """Strategy B taker orders must not drag down the A+C maker ratio."""
    ledger = make_ledger()
    record_rebate(ledger, "f1", strategy="A", is_maker=True)
    record_rebate(ledger, "f2", strategy="B", is_maker=False)
    # B is excluded; only A fill counted → 100%
    assert ledger.maker_ratio(strategies=("A", "C")) == pytest.approx(1.0)


# ── Rebate amounts ────────────────────────────────────────────────────────────

def test_total_rebates_today_sums_amounts():
    ledger = make_ledger()
    record_rebate(ledger, "f1", rebate_amount=0.10)
    record_rebate(ledger, "f2", rebate_amount=0.20)
    assert ledger.total_rebates_today() == pytest.approx(0.30)


# ── Daily reward accumulation ─────────────────────────────────────────────────

def test_total_rewards_today_sums_accumulated():
    ledger = make_ledger()
    record_snapshot(ledger, "tok1", accumulated_today=1.50)
    record_snapshot(ledger, "tok2", accumulated_today=2.50)
    assert ledger.total_rewards_today() == pytest.approx(4.0)


# ── Redemptions (FR-506) ──────────────────────────────────────────────────────

def test_is_redeemed_false_before_recording():
    ledger = make_ledger()
    assert ledger.is_redeemed("cond1") is False


def test_record_redemption_marks_as_redeemed():
    ledger = make_ledger()
    ledger.record_redemption(
        condition_id="cond1",
        token_id="tok1",
        usdc_received=5.00,
        tx_hash="0xabc",
    )
    assert ledger.is_redeemed("cond1") is True


def test_double_redemption_prevention():
    """Second call to record_redemption overwrites but is_redeemed stays True."""
    ledger = make_ledger()
    ledger.record_redemption(condition_id="cond1", token_id="tok1", usdc_received=5.0)
    ledger.record_redemption(condition_id="cond1", token_id="tok1", usdc_received=5.0)
    assert ledger.is_redeemed("cond1") is True


def test_all_redemptions_returns_all():
    ledger = make_ledger()
    ledger.record_redemption(condition_id="c1", token_id="t1", usdc_received=1.0)
    ledger.record_redemption(condition_id="c2", token_id="t2", usdc_received=2.0)
    assert len(ledger.all_redemptions()) == 2
