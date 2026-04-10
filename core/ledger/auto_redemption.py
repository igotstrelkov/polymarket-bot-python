"""
Auto-Redemption — CTF contract redeemPositions() with retry logic.

FR-215:
  1. Poll Gamma API for resolved=true at REDEMPTION_POLL_INTERVAL_S.
  2. Call CTF contract: redeemPositions(collateralToken, parentCollectionId,
     conditionId, indexSets) where:
       collateralToken    = USDC.e address on Polygon
       parentCollectionId = bytes32(0)
       conditionId        = market.condition_id
       indexSets          = [1 << winning_outcome_index]  (bitmask)
  3. Submit via Builder Relayer (gasless).
  4. Submit within 1 hour of resolution confirmation.
  5. Retry up to 3 times: 30s → 120s → 300s backoff.
  6. After 3 failures: log + alert for manual redemption.
  7. Write condition_id to ledger immediately after confirmation (FR-506).
  8. Alert on each successful redemption.

Second call for same condition_id is a no-op (ledger check first).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# FR-215 retry backoff seconds
_RETRY_DELAYS_S = (30, 120, 300)

# USDC.e on Polygon
_USDC_E_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# bytes32(0) — zero parent collection ID for standard binary markets
_ZERO_BYTES32 = b"\x00" * 32


@dataclass
class RedemptionRequest:
    condition_id: str
    token_id: str
    winning_outcome_index: int   # 0 for YES, 1 for NO
    market_name: str = ""


async def auto_redeem(
    request: RedemptionRequest,
    *,
    relayer_client: Any,    # has post_transaction(tx_dict) → tx_hash
    reward_ledger: Any,     # RewardAndRebateLedger — checked for double-redeem
    alerter: Any,           # Alerter
    usdc_received: float = 0.0,
) -> bool:
    """Execute CTF redemption with retry. Returns True on success.

    FR-506: condition_id is written to the ledger before this function returns
    on success, preventing double-redemption across restarts.
    """
    # FR-506: double-redemption guard
    if reward_ledger.is_redeemed(request.condition_id):
        log.info(
            "auto_redeem: %s already redeemed — skipping",
            request.condition_id,
        )
        return True

    # indexSets: bitmask with bit at winning_outcome_index set
    index_sets = [1 << request.winning_outcome_index]

    tx_payload = _build_redeem_payload(request.condition_id, index_sets)

    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS_S, start=1):
        try:
            tx_hash = await relayer_client.post_transaction(tx_payload)
            # Success — write to ledger immediately (FR-506)
            reward_ledger.record_redemption(
                condition_id=request.condition_id,
                token_id=request.token_id,
                usdc_received=usdc_received,
                tx_hash=tx_hash or "",
            )
            log.info(
                "auto_redeem: success condition=%s tx=%s usdc=%.2f",
                request.condition_id, tx_hash, usdc_received,
            )
            await alerter.redemption_success(request.condition_id, usdc_received)
            return True

        except Exception as exc:
            last_exc = exc
            log.warning(
                "auto_redeem: attempt %d/%d failed for %s: %s",
                attempt, len(_RETRY_DELAYS_S), request.condition_id, exc,
            )
            if attempt < len(_RETRY_DELAYS_S):
                await asyncio.sleep(delay)

    # All retries exhausted
    log.error(
        "auto_redeem: all %d attempts failed for %s — manual action required: %s",
        len(_RETRY_DELAYS_S), request.condition_id, last_exc,
    )
    await alerter.redemption_failed(request.condition_id, attempts=len(_RETRY_DELAYS_S))
    return False


def _build_redeem_payload(condition_id: str, index_sets: list[int]) -> dict:
    """Build the transaction payload for redeemPositions().

    FR-215 signature:
      redeemPositions(
        address collateralToken,          # USDC.e
        bytes32 parentCollectionId,       # bytes32(0)
        bytes32 conditionId,
        uint256[] indexSets
      )
    """
    return {
        "function": "redeemPositions",
        "collateralToken": _USDC_E_POLYGON,
        "parentCollectionId": _ZERO_BYTES32.hex(),
        "conditionId": condition_id,
        "indexSets": index_sets,
    }
