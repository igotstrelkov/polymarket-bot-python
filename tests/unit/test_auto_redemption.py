"""
Unit tests for core/ledger/auto_redemption.py.

Covers: double-redemption guard (FR-506), successful redemption payload and
ledger write, retry sequence (30s/120s/300s), final-failure alert, index-set
bitmask, and correct contract-call arguments.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from core.ledger.auto_redemption import (
    RedemptionRequest,
    _build_redeem_payload,
    _USDC_E_POLYGON,
    _ZERO_BYTES32,
    auto_redeem,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_request(
    condition_id: str = "cond_abc",
    token_id: str = "tok_1",
    winning_outcome_index: int = 0,
    market_name: str = "Test Market",
) -> RedemptionRequest:
    return RedemptionRequest(
        condition_id=condition_id,
        token_id=token_id,
        winning_outcome_index=winning_outcome_index,
        market_name=market_name,
    )


def make_reward_ledger(already_redeemed: bool = False) -> MagicMock:
    ledger = MagicMock()
    ledger.is_redeemed = MagicMock(return_value=already_redeemed)
    ledger.record_redemption = MagicMock()
    return ledger


def make_alerter() -> AsyncMock:
    alerter = AsyncMock()
    alerter.redemption_success = AsyncMock()
    alerter.redemption_failed = AsyncMock()
    return alerter


def make_relayer(tx_hash: str = "0xdeadbeef") -> AsyncMock:
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(return_value=tx_hash)
    return relayer


def make_failing_relayer(exc: Exception | None = None) -> AsyncMock:
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(
        side_effect=exc or Exception("relayer error")
    )
    return relayer


# ── Double-redemption guard (FR-506) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_already_redeemed_returns_true_immediately():
    """Second call for same condition_id is a no-op."""
    reward_ledger = make_reward_ledger(already_redeemed=True)
    relayer = make_relayer()
    alerter = make_alerter()

    result = await auto_redeem(
        make_request(),
        relayer_client=relayer,
        reward_ledger=reward_ledger,
        alerter=alerter,
    )

    assert result is True
    relayer.post_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_already_redeemed_does_not_alert():
    """No alert should fire on the no-op path."""
    reward_ledger = make_reward_ledger(already_redeemed=True)
    alerter = make_alerter()

    await auto_redeem(
        make_request(),
        relayer_client=make_relayer(),
        reward_ledger=reward_ledger,
        alerter=alerter,
    )

    alerter.redemption_success.assert_not_called()
    alerter.redemption_failed.assert_not_called()


# ── Successful redemption ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_successful_redemption_returns_true():
    result = await auto_redeem(
        make_request(),
        relayer_client=make_relayer(),
        reward_ledger=make_reward_ledger(),
        alerter=make_alerter(),
    )
    assert result is True


@pytest.mark.asyncio
async def test_successful_redemption_writes_to_ledger():
    """FR-506: condition_id written to ledger immediately after success."""
    reward_ledger = make_reward_ledger()
    req = make_request(condition_id="cond_xyz", token_id="tok_99")

    await auto_redeem(
        req,
        relayer_client=make_relayer(tx_hash="0xabc"),
        reward_ledger=reward_ledger,
        alerter=make_alerter(),
        usdc_received=50.0,
    )

    reward_ledger.record_redemption.assert_called_once_with(
        condition_id="cond_xyz",
        token_id="tok_99",
        usdc_received=50.0,
        tx_hash="0xabc",
    )


@pytest.mark.asyncio
async def test_successful_redemption_sends_success_alert():
    alerter = make_alerter()
    req = make_request(condition_id="cond_1")

    await auto_redeem(
        req,
        relayer_client=make_relayer(),
        reward_ledger=make_reward_ledger(),
        alerter=alerter,
        usdc_received=25.0,
    )

    alerter.redemption_success.assert_awaited_once_with("cond_1", 25.0)


@pytest.mark.asyncio
async def test_successful_redemption_calls_relayer_once():
    relayer = make_relayer()
    await auto_redeem(
        make_request(),
        relayer_client=relayer,
        reward_ledger=make_reward_ledger(),
        alerter=make_alerter(),
    )
    assert relayer.post_transaction.await_count == 1


# ── Index sets bitmask ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_set_yes_outcome():
    """winning_outcome_index=0 → indexSets=[1]."""
    relayer = make_relayer()
    await auto_redeem(
        make_request(winning_outcome_index=0),
        relayer_client=relayer,
        reward_ledger=make_reward_ledger(),
        alerter=make_alerter(),
    )
    payload = relayer.post_transaction.call_args[0][0]
    assert payload["indexSets"] == [1]


@pytest.mark.asyncio
async def test_index_set_no_outcome():
    """winning_outcome_index=1 → indexSets=[2]."""
    relayer = make_relayer()
    await auto_redeem(
        make_request(winning_outcome_index=1),
        relayer_client=relayer,
        reward_ledger=make_reward_ledger(),
        alerter=make_alerter(),
    )
    payload = relayer.post_transaction.call_args[0][0]
    assert payload["indexSets"] == [2]


@pytest.mark.asyncio
async def test_index_set_third_outcome():
    """winning_outcome_index=2 → indexSets=[4]."""
    relayer = make_relayer()
    await auto_redeem(
        make_request(winning_outcome_index=2),
        relayer_client=relayer,
        reward_ledger=make_reward_ledger(),
        alerter=make_alerter(),
    )
    payload = relayer.post_transaction.call_args[0][0]
    assert payload["indexSets"] == [4]


# ── Contract call payload ─────────────────────────────────────────────────────

def test_build_redeem_payload_function():
    payload = _build_redeem_payload("cond_abc", [1])
    assert payload["function"] == "redeemPositions"


def test_build_redeem_payload_usdc_address():
    payload = _build_redeem_payload("cond_abc", [1])
    assert payload["collateralToken"] == _USDC_E_POLYGON


def test_build_redeem_payload_zero_parent_collection():
    """parentCollectionId must be bytes32(0) as hex string."""
    payload = _build_redeem_payload("cond_abc", [1])
    expected = _ZERO_BYTES32.hex()
    assert payload["parentCollectionId"] == expected
    # Must be 64 hex characters (32 bytes)
    assert len(payload["parentCollectionId"]) == 64


def test_build_redeem_payload_condition_id():
    payload = _build_redeem_payload("cond_xyz", [2])
    assert payload["conditionId"] == "cond_xyz"


def test_build_redeem_payload_index_sets():
    payload = _build_redeem_payload("cond_abc", [4])
    assert payload["indexSets"] == [4]


def test_usdc_e_polygon_address():
    assert _USDC_E_POLYGON == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


# ── Retry sequence ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_sequence_delays():
    """Retries should sleep for 30s then 120s (not 300s — last attempt skips sleep)."""
    relayer = make_failing_relayer()

    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=make_reward_ledger(),
            alerter=make_alerter(),
        )

    # 3 attempts: sleep after attempt 1 (30s), sleep after attempt 2 (120s),
    # no sleep after attempt 3 (last)
    assert mock_sleep.await_count == 2
    sleep_delays = [c.args[0] for c in mock_sleep.await_args_list]
    assert sleep_delays == [30, 120]


@pytest.mark.asyncio
async def test_all_three_attempts_made():
    relayer = make_failing_relayer()

    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock):
        await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=make_reward_ledger(),
            alerter=make_alerter(),
        )

    assert relayer.post_transaction.await_count == 3


@pytest.mark.asyncio
async def test_all_retries_exhausted_returns_false():
    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock):
        result = await auto_redeem(
            make_request(),
            relayer_client=make_failing_relayer(),
            reward_ledger=make_reward_ledger(),
            alerter=make_alerter(),
        )
    assert result is False


@pytest.mark.asyncio
async def test_all_retries_exhausted_sends_failure_alert():
    alerter = make_alerter()

    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock):
        await auto_redeem(
            make_request(condition_id="cond_fail"),
            relayer_client=make_failing_relayer(),
            reward_ledger=make_reward_ledger(),
            alerter=alerter,
        )

    alerter.redemption_failed.assert_awaited_once_with("cond_fail", attempts=3)


@pytest.mark.asyncio
async def test_failure_does_not_write_to_ledger():
    reward_ledger = make_reward_ledger()

    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock):
        await auto_redeem(
            make_request(),
            relayer_client=make_failing_relayer(),
            reward_ledger=reward_ledger,
            alerter=make_alerter(),
        )

    reward_ledger.record_redemption.assert_not_called()


@pytest.mark.asyncio
async def test_success_on_second_attempt():
    """If attempt 1 fails and attempt 2 succeeds, returns True."""
    relayer = AsyncMock()
    relayer.post_transaction = AsyncMock(
        side_effect=[Exception("first fail"), "0xtxhash"]
    )
    reward_ledger = make_reward_ledger()
    alerter = make_alerter()

    with patch("core.ledger.auto_redemption.asyncio.sleep", new_callable=AsyncMock):
        result = await auto_redeem(
            make_request(),
            relayer_client=relayer,
            reward_ledger=reward_ledger,
            alerter=alerter,
        )

    assert result is True
    reward_ledger.record_redemption.assert_called_once()
    alerter.redemption_success.assert_awaited_once()
    alerter.redemption_failed.assert_not_called()
