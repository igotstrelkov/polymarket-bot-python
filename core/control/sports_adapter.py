"""Sports Market Adapter — FR-119, §4.4.2, Design Principle P5.

Isolates sports-specific execution rules so that sports markets do not share
identical execution logic with other market types:
  1. All open quotes are cancelled when game_start_time is reached/passed.
  2. GTD expiry for new quotes must be set before game_start_time.
  3. Marketable orders carry an additional 3-second processing delay.

The v3 sports adapter derives all behavior from game_start_time and Gamma market
metadata — the conditional Sports WebSocket channel is not required.
"""

from __future__ import annotations

import time
from datetime import datetime


def is_sports_market(game_start_time: datetime | None) -> bool:
    """True when the market has a non-null game_start_time (FR-119).

    All markets with a non-null game_start_time must be routed through this adapter.
    """
    return game_start_time is not None


def should_cancel_at_game_start(
    game_start_time: datetime | None,
    now_ms: float | None = None,
) -> bool:
    """True when game_start_time is in the past — all open quotes must be cancelled.

    The platform automatically cancels outstanding limit orders at game start (FR-119);
    this function drives the bot's own pre-emptive cancellation pass.
    """
    if game_start_time is None:
        return False
    if now_ms is None:
        now_ms = time.time() * 1000
    return game_start_time.timestamp() * 1000 <= now_ms


def compute_gtd_before_game_start(
    game_start_time: datetime,
    gtd_game_start_buffer_ms: int = 300_000,
) -> int:
    """Compute GTD expiry so the order expires before game_start_time.

    Formula: game_start_unix - (buffer_ms // 1000) + 60

    The +60 accounts for the platform's 1-minute security threshold: the effective
    lifetime of a GTD order is the specified expiration minus 60 seconds (FR-201).
    Default buffer: GTD_GAME_START_BUFFER_MS = 300_000ms (5 minutes).
    """
    return int(game_start_time.timestamp() - (gtd_game_start_buffer_ms // 1000) + 60)


def marketable_order_delay_ms() -> int:
    """Return the additional processing delay for marketable orders on sports markets.

    Sports markets carry a documented 3-second delay for marketable orders (FR-119).
    This must be accounted for in execution latency budgets.
    """
    return 3_000
