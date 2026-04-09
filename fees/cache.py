"""
FeeRateCache — per-token fee rate cache with TTL, miss tracking, and deviation detection.

FR-151: TTL = FEE_CACHE_TTL_S (30s). Re-fetch on fill events. Invalidate on >10% deviation.
FR-156: On invalidation triggered by fill or deviation, cancel quotes within one event cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import httpx

log = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    bps: int
    expires_at: float


class FeeRateCache:
    """Per-token fee rate cache.

    Thread-safety: designed for single-threaded asyncio use — no locking.
    """

    def __init__(
        self,
        ttl_s: int = 30,
        consecutive_miss_threshold: int = 5,
        deviation_threshold_pct: float = 10.0,
    ) -> None:
        self._ttl_s = ttl_s
        self._miss_threshold = consecutive_miss_threshold
        self._deviation_threshold_pct = deviation_threshold_pct

        self._entries: dict[str, _CacheEntry] = {}
        self._miss_counts: dict[str, int] = {}

        # Optional async re-fetch callback injected at startup.
        # Signature: async (token_id: str) -> int  (returns bps)
        self._refetch_fn: Callable[[str], Coroutine[Any, Any, int]] | None = None

    def set_refetch_fn(
        self, fn: Callable[[str], Coroutine[Any, Any, int]]
    ) -> None:
        """Inject the async re-fetch function used by on_fill()."""
        self._refetch_fn = fn

    # ── Read / Write ──────────────────────────────────────────────────────────

    def get(self, token_id: str) -> int | None:
        """Return cached bps if present and not expired; otherwise None."""
        entry = self._entries.get(token_id)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._entries[token_id]
            return None
        return entry.bps

    def set(self, token_id: str, bps: int) -> None:
        """Cache fee rate with TTL. Resets consecutive miss counter."""
        self._entries[token_id] = _CacheEntry(
            bps=bps,
            expires_at=time.monotonic() + self._ttl_s,
        )
        self._miss_counts.pop(token_id, None)

    def invalidate(self, token_id: str) -> None:
        """Remove entry from cache immediately."""
        self._entries.pop(token_id, None)

    # ── Miss tracking ─────────────────────────────────────────────────────────

    def record_miss(self, token_id: str) -> None:
        """Increment consecutive miss counter for this token."""
        self._miss_counts[token_id] = self._miss_counts.get(token_id, 0) + 1
        log.debug(
            "Fee cache miss #%d for %s", self._miss_counts[token_id], token_id
        )

    def should_exclude(self, token_id: str) -> bool:
        """True when consecutive misses >= FEE_CONSECUTIVE_MISS_THRESHOLD."""
        return self._miss_counts.get(token_id, 0) >= self._miss_threshold

    # ── Deviation detection ───────────────────────────────────────────────────

    def check_deviation(self, token_id: str, new_bps: int) -> bool:
        """Return True if new_bps differs from cached value by > deviation_threshold_pct.

        Also invalidates the cache entry if deviation is detected (FR-151).
        """
        cached = self.get(token_id)
        if cached is None or cached == 0:
            return False
        deviation_pct = abs(new_bps - cached) / cached * 100
        if deviation_pct > self._deviation_threshold_pct:
            log.warning(
                "Fee rate deviation for %s: cached=%d new=%d (%.1f%% > %.1f%%)",
                token_id, cached, new_bps, deviation_pct, self._deviation_threshold_pct,
            )
            self.invalidate(token_id)
            return True
        return False

    # ── Fill event handler ────────────────────────────────────────────────────

    async def on_fill(self, token_id: str) -> None:
        """Trigger immediate async re-fetch on fill event (FR-151).

        Overrides cached value with freshly fetched rate.
        No-op if no refetch function is registered.
        """
        if self._refetch_fn is None:
            log.debug("on_fill: no refetch function registered for %s", token_id)
            return
        try:
            new_bps = await self._refetch_fn(token_id)
            self.set(token_id, new_bps)
            log.debug("on_fill: refreshed fee rate for %s → %d bps", token_id, new_bps)
        except Exception as exc:
            log.warning("on_fill: re-fetch failed for %s: %s", token_id, exc)


# ── Fee fetch helper ──────────────────────────────────────────────────────────

async def fetch_fee_rate(
    token_id: str,
    http_client: httpx.AsyncClient,
    clob_host: str,
    hmac_headers: dict,
) -> int:
    """Fetch current fee rate from CLOB API.

    Endpoint: GET /fee-rate/{token_id}
    Response field: 'base_fee'  ← NOT 'feeRateBps'

    Always normalise at this boundary — return value is stored as fee_rate_bps.
    """
    r = await http_client.get(
        f"{clob_host}/fee-rate/{token_id}",
        headers=hmac_headers,
    )
    r.raise_for_status()
    return int(r.json()["base_fee"])
