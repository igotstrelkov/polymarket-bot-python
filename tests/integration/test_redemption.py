"""
Integration tests for CTF auto-redemption (FR-215, FR-506).

Covers:
- redeemPositions() called with correct 4-arg payload
- Successful redemption within expected latency (no timeout)
- 3 retries at 30s/120s delays; alert on final failure
- Same condition_id second call → no-op (FR-506 double-redemption guard)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from core.ledger.auto_redemption import (
    RedemptionRequest,
    _USDC_E_POLYGON,
    _ZERO_BYTES32,
    auto_redeem,
)
from core.ledger.reward_rebate_ledger import RewardAndRebateLedger


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_request(
    condition_id: str = "cond_abc",
    token_id: str = "tok_1",
    winning_outcome_index: int = 0,
) -> RedemptionRequest:
    return RedemptionRequest(
        condition_id=condition_id,
        token_id=token_id,
        winning_outcome_index=winning_outcome_index,
        market_name="Test Market",
    )


def make_reward_ledger() -> RewardAndRebateLedger:
    return RewardAndRebateLedger()


def make_alerter() -> AsyncMock:
    alerter = AsyncMock()
    alerter.redemption_success = AsyncMock()
    alerter.redemption_failed = AsyncMock()
    return alerter


# ── Test 1: correct 4-arg payload ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redeem_payload_has_correct_function_name():
    """redeemPositions is the function name in the tx payload."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(winning_outcome_index=0),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["function"] == "redeemPositions"


@pytest.mark.asyncio
async def test_redeem_payload_collateral_token():
    """Payload collateralToken must be the USDC.e Polygon address."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["collateralToken"] == _USDC_E_POLYGON


@pytest.mark.asyncio
async def test_redeem_payload_parent_collection_id():
    """Payload parentCollectionId must be bytes32(0) hex."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["parentCollectionId"] == _ZERO_BYTES32.hex()


@pytest.mark.asyncio
async def test_redeem_payload_condition_id():
    """Payload conditionId matches the request's condition_id."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(condition_id="cond_xyz"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["conditionId"] == "cond_xyz"


@pytest.mark.asyncio
async def test_redeem_payload_index_sets_yes_wins():
    """winning_outcome_index=0 → indexSets=[1] (bit 0 set)."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(winning_outcome_index=0),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["indexSets"] == [1]  # 1 << 0


@pytest.mark.asyncio
async def test_redeem_payload_index_sets_no_wins():
    """winning_outcome_index=1 → indexSets=[2] (bit 1 set)."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xabc")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(winning_outcome_index=1),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    payload = relayer.post_transaction.call_args[0][0]
    assert payload["indexSets"] == [2]  # 1 << 1


# ── Test 2: successful redemption on first attempt ────────────────────────────

@pytest.mark.asyncio
async def test_successful_redemption_returns_true():
    """auto_redeem returns True on first-attempt success."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    result = await auto_redeem(
        make_request(),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    assert result is True


@pytest.mark.asyncio
async def test_successful_redemption_writes_to_ledger():
    """Redemption writes condition_id to ledger (FR-506)."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(condition_id="cond_abc"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    assert ledger.is_redeemed("cond_abc") is True


@pytest.mark.asyncio
async def test_successful_redemption_calls_alerter():
    """Successful redemption calls alerter.redemption_success."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(condition_id="cond_abc"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
        usdc_received=12.5,
    )

    alerter.redemption_success.assert_awaited_once_with("cond_abc", 12.5)


# ── Test 3: 3 retries with correct sleep delays ───────────────────────────────

@pytest.mark.asyncio
async def test_all_three_attempts_made_on_failure():
    """All 3 attempts are made when each one fails."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(side_effect=Exception("tx failed"))
    ledger = make_reward_ledger()
    alerter = make_alerter()

    with patch("core.ledger.auto_redemption.asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        result = await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    assert result is False
    assert relayer.post_transaction.call_count == 3


@pytest.mark.asyncio
async def test_retry_sleep_delays_are_30_then_120():
    """Sleep delays after attempt 1 and 2 are 30s and 120s."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(side_effect=Exception("tx failed"))
    ledger = make_reward_ledger()
    alerter = make_alerter()

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("core.ledger.auto_redemption.asyncio.sleep", side_effect=fake_sleep):
        await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    # 3 attempts → 2 sleeps (after attempt 1 and 2, not after 3)
    assert sleep_calls == [30, 120]


@pytest.mark.asyncio
async def test_no_sleep_after_final_attempt():
    """No sleep after the last (3rd) attempt."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(side_effect=Exception("tx failed"))
    ledger = make_reward_ledger()
    alerter = make_alerter()

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("core.ledger.auto_redemption.asyncio.sleep", side_effect=fake_sleep):
        await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    assert len(sleep_calls) == 2  # exactly 2, not 3


@pytest.mark.asyncio
async def test_alert_sent_on_final_failure():
    """alerter.redemption_failed is called after all 3 attempts fail."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(side_effect=Exception("tx failed"))
    ledger = make_reward_ledger()
    alerter = make_alerter()

    with patch("core.ledger.auto_redemption.asyncio.sleep"):
        await auto_redeem(
            make_request(condition_id="cond_fail"),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    alerter.redemption_failed.assert_awaited_once_with("cond_fail", attempts=3)


@pytest.mark.asyncio
async def test_ledger_not_written_on_failure():
    """Ledger must not record redemption when all attempts fail."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(side_effect=Exception("tx failed"))
    ledger = make_reward_ledger()
    alerter = make_alerter()

    with patch("core.ledger.auto_redemption.asyncio.sleep"):
        await auto_redeem(
            make_request(condition_id="cond_fail"),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    assert ledger.is_redeemed("cond_fail") is False


@pytest.mark.asyncio
async def test_success_on_second_attempt():
    """Success on 2nd attempt returns True; only 1 sleep before success."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(
        side_effect=[Exception("fail"), "0xtx_ok"]
    )
    ledger = make_reward_ledger()
    alerter = make_alerter()

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("core.ledger.auto_redemption.asyncio.sleep", side_effect=fake_sleep):
        result = await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=ledger,
            alerter=alerter,
        )

    assert result is True
    assert sleep_calls == [30]


# ── Test 4: same condition_id second call → no-op (FR-506) ───────────────────

@pytest.mark.asyncio
async def test_second_call_same_condition_id_is_noop():
    """Second call for same condition_id returns True without calling relayer."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    # First call succeeds
    await auto_redeem(
        make_request(condition_id="cond_dup"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    # Second call must be a no-op
    result = await auto_redeem(
        make_request(condition_id="cond_dup"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    assert result is True
    # relayer called only once (first attempt), not twice
    assert relayer.post_transaction.call_count == 1


@pytest.mark.asyncio
async def test_noop_does_not_call_alerter_again():
    """No-op on duplicate condition_id does not re-send redemption_success alert."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(condition_id="cond_dup"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )
    first_call_count = alerter.redemption_success.await_count

    await auto_redeem(
        make_request(condition_id="cond_dup"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    # Alert count must not have increased
    assert alerter.redemption_success.await_count == first_call_count


@pytest.mark.asyncio
async def test_different_condition_ids_each_processed():
    """Two different condition_ids are each redeemed independently."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value="0xtx1")
    ledger = make_reward_ledger()
    alerter = make_alerter()

    await auto_redeem(
        make_request(condition_id="cond_1"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )
    await auto_redeem(
        make_request(condition_id="cond_2"),
        relayer_client=relayer,
        reward_ledger=ledger,
        alerter=alerter,
    )

    assert relayer.post_transaction.call_count == 2
    assert ledger.is_redeemed("cond_1") is True
    assert ledger.is_redeemed("cond_2") is True
