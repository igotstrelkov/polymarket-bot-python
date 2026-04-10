"""
Reward and Rebate Ledger — persists per-market liquidity reward percentages,
daily earnings, order scoring snapshots, and maker rebates.

FR-453: Record actual maker/taker classification per fill for A+C strategies.
FR-506: Write redeemed market IDs to ledger after on-chain confirmation.
FR-158: Track order scoring status; flag unscored orders in daily summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class RewardSnapshot:
    """Periodic snapshot of reward state for a market (FR-158)."""
    token_id: str
    snapshot_at: datetime
    rewards_percentage: float | None    # user's current reward %
    is_scored: bool                     # order scoring status
    daily_rate: float | None            # rewardsDailyRate from CLOB
    accumulated_today: float = 0.0


@dataclass
class RebateRecord:
    """Per-fill maker rebate attribution."""
    fill_id: str
    token_id: str
    strategy: str
    is_maker: bool
    rebate_amount: float    # USDC earned (negative = taker fee paid)
    recorded_at: datetime


@dataclass
class RedemptionRecord:
    """FR-506: on-chain redemption record to prevent double-redemption."""
    condition_id: str
    token_id: str
    redeemed_at: datetime
    usdc_received: float
    tx_hash: str = ""


class RewardAndRebateLedger:
    """In-process reward and rebate ledger.

    Tracks:
      - Per-market reward snapshots and daily accumulation
      - Per-fill rebate/fee records for maker ratio reporting
      - Redeemed market IDs (FR-506)
    """

    def __init__(self) -> None:
        # token_id → latest RewardSnapshot
        self._reward_snapshots: dict[str, RewardSnapshot] = {}
        # token_id → daily accumulation bucket {date: amount}
        self._daily_rewards: dict[str, dict[date, float]] = {}
        # fill_id → RebateRecord
        self._rebates: list[RebateRecord] = []
        # condition_id → RedemptionRecord (FR-506 double-redemption prevention)
        self._redemptions: dict[str, RedemptionRecord] = {}

    # ── Reward snapshots (FR-158) ─────────────────────────────────────────────

    def record_reward_snapshot(
        self,
        *,
        token_id: str,
        rewards_percentage: float | None,
        is_scored: bool,
        daily_rate: float | None,
        accumulated_today: float = 0.0,
    ) -> RewardSnapshot:
        now = datetime.now(tz=timezone.utc)
        snap = RewardSnapshot(
            token_id=token_id,
            snapshot_at=now,
            rewards_percentage=rewards_percentage,
            is_scored=is_scored,
            daily_rate=daily_rate,
            accumulated_today=accumulated_today,
        )
        self._reward_snapshots[token_id] = snap

        # Accumulate daily bucket
        today = now.date()
        bucket = self._daily_rewards.setdefault(token_id, {})
        bucket[today] = bucket.get(today, 0.0) + accumulated_today

        return snap

    def get_reward_snapshot(self, token_id: str) -> RewardSnapshot | None:
        return self._reward_snapshots.get(token_id)

    def unscored_tokens(self) -> list[str]:
        """Tokens whose latest reward snapshot shows is_scored=False (for daily summary)."""
        return [
            token_id
            for token_id, snap in self._reward_snapshots.items()
            if not snap.is_scored
        ]

    def total_rewards_today(self) -> float:
        today = datetime.now(tz=timezone.utc).date()
        return sum(
            bucket.get(today, 0.0)
            for bucket in self._daily_rewards.values()
        )

    # ── Maker rebates (FR-453) ────────────────────────────────────────────────

    def record_rebate(
        self,
        *,
        fill_id: str,
        token_id: str,
        strategy: str,
        is_maker: bool,
        rebate_amount: float,
    ) -> RebateRecord:
        rec = RebateRecord(
            fill_id=fill_id,
            token_id=token_id,
            strategy=strategy,
            is_maker=is_maker,
            rebate_amount=rebate_amount,
            recorded_at=datetime.now(tz=timezone.utc),
        )
        self._rebates.append(rec)
        if not is_maker and strategy in ("A", "C"):
            log.warning(
                "RewardRebateLedger: taker fill on Post-Only order fill=%s strategy=%s",
                fill_id, strategy,
            )
        return rec

    def maker_ratio(self, strategies: tuple[str, ...] = ("A", "C")) -> float:
        """Maker ratio (0–1) across the specified strategies.  Returns 1.0 if no fills."""
        relevant = [r for r in self._rebates if r.strategy in strategies]
        if not relevant:
            return 1.0
        return sum(1 for r in relevant if r.is_maker) / len(relevant)

    def total_rebates_today(self) -> float:
        today = datetime.now(tz=timezone.utc).date()
        return sum(
            r.rebate_amount
            for r in self._rebates
            if r.recorded_at.date() == today
        )

    # ── Redemptions (FR-506) ──────────────────────────────────────────────────

    def record_redemption(
        self,
        *,
        condition_id: str,
        token_id: str,
        usdc_received: float,
        tx_hash: str = "",
    ) -> RedemptionRecord:
        rec = RedemptionRecord(
            condition_id=condition_id,
            token_id=token_id,
            redeemed_at=datetime.now(tz=timezone.utc),
            usdc_received=usdc_received,
            tx_hash=tx_hash,
        )
        self._redemptions[condition_id] = rec
        log.info(
            "RewardRebateLedger: redeemed condition=%s usdc=%.2f tx=%s",
            condition_id, usdc_received, tx_hash,
        )
        return rec

    def is_redeemed(self, condition_id: str) -> bool:
        """FR-506: True if condition_id has already been redeemed."""
        return condition_id in self._redemptions

    def all_redemptions(self) -> list[RedemptionRecord]:
        return list(self._redemptions.values())
