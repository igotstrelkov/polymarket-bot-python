"""
Universe Scanner — full-catalog Gamma API discovery with mutation detection
and resolution watchlist management.

FR-101: paginate Gamma API (50/page) for all active, non-closed events/markets.
FR-102: extract token IDs, tick sizes, minimum_order_size, seconds_delay,
        negRisk, game_start_time, volume, tags, accepting_orders.
FR-103: rescan every SCAN_INTERVAL_MS (default 5 min).
FR-103a: detect mutations between cycles; surface via on_mutation callback.
FR-505: maintain resolution watchlist — within 2h no new entries,
        within 30min force-cancel all quotes.
FR-215: resolution confirmation polling at REDEMPTION_POLL_INTERVAL_S.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from core.control.capability_enricher import (
    MarketCapabilityModel,
    MutationType,
    detect_mutations,
    enrich,
)

log = logging.getLogger(__name__)

_GAMMA_API_HOST = "https://gamma-api.polymarket.com"
_PAGE_SIZE = 50

# Resolution watchlist thresholds (ms)
_RESOLUTION_WARN_MS  = 7_200_000   # 2 hours — no new entries
_RESOLUTION_PULL_MS  = 1_800_000   # 30 minutes — force cancel


@dataclass
class ResolutionWatchlistEntry:
    token_id: str
    condition_id: str
    resolution_time: datetime
    no_new_entries: bool = False
    force_cancel: bool = False


@dataclass
class UniverseScanner:
    """Discovers and tracks all Polymarket markets via the Gamma API.

    Usage:
        scanner = UniverseScanner(http_client=..., fee_cache=..., settings=...)
        markets = await scanner.scan_once()
        await scanner.run_forever()   # loops at SCAN_INTERVAL_MS

    Inject `on_mutation` to react to market metadata changes (FR-103a).
    Inject `on_resolution_watchlist_update` to react to watchlist changes.
    """

    http_client: Any   # httpx.AsyncClient or similar — passed in by caller
    fee_cache: Any     # FeeRateCache — used to warm fee rates during scan
    scan_interval_ms: int = 300_000
    redemption_poll_interval_s: int = 60
    universe_tags: list[str] = field(default_factory=list)

    # Callbacks — set by the orchestrator after construction
    on_mutation: Callable[[str, list[MutationType]], Awaitable[None]] | None = None
    on_resolution_watchlist_update: Callable[
        [list[ResolutionWatchlistEntry]], Awaitable[None]
    ] | None = None

    # Internal state
    _previous: dict[str, MarketCapabilityModel] = field(default_factory=dict, repr=False)
    _watchlist: dict[str, ResolutionWatchlistEntry] = field(default_factory=dict, repr=False)
    _running: bool = field(default=False, repr=False)

    # ── Public API ────────────────────────────────────────────────────────────

    async def scan_once(self) -> list[MarketCapabilityModel]:
        """Fetch all active markets, enrich them, detect mutations.

        Returns the full enriched candidate list for this cycle.
        """
        raw_markets = await self._fetch_all_markets()
        current: dict[str, MarketCapabilityModel] = {}

        for raw in raw_markets:
            token_id = raw.get("token_id") or raw.get("clobTokenIds", [""])[0]
            if not token_id:
                continue

            fee_rate_bps = self.fee_cache.get(token_id) or 0

            try:
                model = enrich(raw, fee_rate_bps=fee_rate_bps)
            except Exception:
                log.exception("Failed to enrich market %s", token_id)
                continue

            current[token_id] = model

            # FR-103a: mutation detection
            if token_id in self._previous:
                mutations = detect_mutations(self._previous[token_id], model)
                if mutations and self.on_mutation:
                    await self.on_mutation(token_id, mutations)

        self._previous = current
        await self._update_watchlist(list(current.values()))

        log.info("UniverseScanner: scanned %d markets", len(current))
        return list(current.values())

    async def stop(self) -> None:
        self._running = False

    # ── Resolution watchlist ──────────────────────────────────────────────────

    async def _update_watchlist(self, markets: list[MarketCapabilityModel]) -> None:
        """FR-505: update the resolution watchlist from the current market snapshot."""
        now_ms = time.time() * 1000
        updated: dict[str, ResolutionWatchlistEntry] = {}

        for market in markets:
            if market.resolution_time is None:
                continue
            res_ms = market.resolution_time.timestamp() * 1000
            time_to_res = res_ms - now_ms
            if time_to_res <= 0:
                # Already resolved or very close — skip
                continue

            # Only track markets within the warn window (≤ 2h)
            if time_to_res > _RESOLUTION_WARN_MS:
                continue

            no_new = True   # all entries are within the warn window by definition
            force   = time_to_res <= _RESOLUTION_PULL_MS

            updated[market.token_id] = ResolutionWatchlistEntry(
                token_id=market.token_id,
                condition_id=market.condition_id,
                resolution_time=market.resolution_time,
                no_new_entries=no_new,
                force_cancel=force,
            )

        self._watchlist = updated

        if self.on_resolution_watchlist_update:
            await self.on_resolution_watchlist_update(list(updated.values()))

    def watchlist(self) -> list[ResolutionWatchlistEntry]:
        return list(self._watchlist.values())

    def is_within_warn_window(self, token_id: str) -> bool:
        """True when the market is within RESOLUTION_WARN_MS of resolution (no new entries)."""
        entry = self._watchlist.get(token_id)
        return entry is not None and entry.no_new_entries

    def is_within_pull_window(self, token_id: str) -> bool:
        """True when the market is within RESOLUTION_PULL_MS of resolution (force cancel)."""
        entry = self._watchlist.get(token_id)
        return entry is not None and entry.force_cancel

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    async def _fetch_all_markets(self) -> list[dict]:
        """Paginate the Gamma API and return all raw market dicts (FR-101)."""
        markets: list[dict] = []
        offset = 0

        while True:
            params: dict[str, Any] = {
                "limit": _PAGE_SIZE,
                "offset": offset,
                "active": "true",
                "closed": "false",
            }
            if self.universe_tags:
                params["tag_slug"] = ",".join(self.universe_tags)

            try:
                resp = await self.http_client.get(
                    f"{_GAMMA_API_HOST}/markets",
                    params=params,
                )
                resp.raise_for_status()
                page: list[dict] = resp.json()
            except Exception:
                log.exception("UniverseScanner: failed to fetch markets at offset=%d", offset)
                break

            if not page:
                break

            # Flatten multi-outcome markets to one entry per token
            for market in page:
                clob_ids: list[str] = market.get("clobTokenIds") or []
                if not clob_ids:
                    markets.append(market)
                    continue
                for token_id in clob_ids:
                    markets.append({**market, "token_id": token_id})

            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        return markets
