"""
Unit tests for core/control/universe_scanner.py.

Covers: Gamma API pagination (FR-101), multi-outcome market flattening,
mutation detection callbacks (FR-103a), and resolution watchlist management
(FR-505: 2h no-new-entries / 30min force-cancel thresholds).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.control.capability_enricher import MarketCapabilityModel, MutationType
from core.control.universe_scanner import (
    ResolutionWatchlistEntry,
    UniverseScanner,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_raw_market(
    token_id: str = "tok1",
    *,
    clob_ids: list[str] | None = None,
    accepting_orders: bool = True,
    resolution_time: str | None = None,
) -> dict:
    m: dict = {
        "conditionId": "cond1",
        "acceptingOrders": accepting_orders,
        "tickSize": "0.01",
        "minimumOrderSize": "1",
        "negRisk": False,
        "feesEnabled": False,
        "secondsDelay": 0,
    }
    if clob_ids is not None:
        m["clobTokenIds"] = clob_ids
    else:
        m["token_id"] = token_id
    if resolution_time:
        m["resolutionTime"] = resolution_time
    return m


def make_http_client(pages: list[list[dict]]) -> AsyncMock:
    """Mock httpx-like client returning successive pages then empty."""
    responses: list[AsyncMock] = []
    for page in pages:
        resp = AsyncMock()
        resp.json = MagicMock(return_value=page)
        resp.raise_for_status = MagicMock()
        responses.append(resp)
    # Final empty page to stop pagination
    empty = AsyncMock()
    empty.json = MagicMock(return_value=[])
    empty.raise_for_status = MagicMock()
    responses.append(empty)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    return client


def make_fee_cache(rate: int = 0) -> MagicMock:
    fc = MagicMock()
    fc.get = MagicMock(return_value=rate)
    return fc


def make_market_model(
    token_id: str = "tok1",
    *,
    accepting_orders: bool = True,
    resolution_time: datetime | None = None,
    fee_rate_bps: int = 0,
) -> MarketCapabilityModel:
    return MarketCapabilityModel(
        token_id=token_id,
        condition_id="cond1",
        tick_size=0.01,
        minimum_order_size=1.0,
        neg_risk=False,
        fees_enabled=False,
        fee_rate_bps=fee_rate_bps,
        seconds_delay=0,
        accepting_orders=accepting_orders,
        game_start_time=None,
        resolution_time=resolution_time,
        rewards_min_size=None,
        rewards_max_spread=None,
        rewards_daily_rate=None,
        adjusted_midpoint=None,
        tags=[],
    )


# ── Pagination (FR-101) ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_once_single_page():
    page = [make_raw_market("t1"), make_raw_market("t2")]
    scanner = UniverseScanner(
        http_client=make_http_client([page]),
        fee_cache=make_fee_cache(),
    )
    markets = await scanner.scan_once()
    assert len(markets) == 2


@pytest.mark.asyncio
async def test_scan_once_multi_page_stops_on_short_page():
    """Two full pages (50 items each) then a short page stops pagination."""
    page1 = [make_raw_market(f"t{i}") for i in range(50)]
    page2 = [make_raw_market(f"t{i}") for i in range(50, 100)]
    page3 = [make_raw_market("t100")]   # short → last page

    scanner = UniverseScanner(
        http_client=make_http_client([page1, page2, page3]),
        fee_cache=make_fee_cache(),
    )
    markets = await scanner.scan_once()
    assert len(markets) == 101


@pytest.mark.asyncio
async def test_scan_once_empty_response_returns_empty():
    scanner = UniverseScanner(
        http_client=make_http_client([[]]),
        fee_cache=make_fee_cache(),
    )
    markets = await scanner.scan_once()
    assert markets == []


# ── Multi-outcome market flattening ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_once_flattens_multi_outcome():
    """A market with clobTokenIds=[YES, NO] is split into two entries."""
    page = [make_raw_market(clob_ids=["yes_tok", "no_tok"])]
    scanner = UniverseScanner(
        http_client=make_http_client([page]),
        fee_cache=make_fee_cache(),
    )
    markets = await scanner.scan_once()
    token_ids = {m.token_id for m in markets}
    assert token_ids == {"yes_tok", "no_tok"}


@pytest.mark.asyncio
async def test_scan_once_single_outcome_no_clob_ids():
    """Market without clobTokenIds is passed through with its own token_id."""
    page = [make_raw_market("solo_tok")]
    scanner = UniverseScanner(
        http_client=make_http_client([page]),
        fee_cache=make_fee_cache(),
    )
    markets = await scanner.scan_once()
    assert len(markets) == 1
    assert markets[0].token_id == "solo_tok"


# ── Mutation detection (FR-103a) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mutation_callback_fires_on_change():
    """Changing fee_rate_bps between scans triggers on_mutation."""
    page = [make_raw_market("tok1")]

    # First scan: fee_rate=0
    client1 = make_http_client([page])
    fc1 = make_fee_cache(rate=0)
    scanner = UniverseScanner(http_client=client1, fee_cache=fc1)
    await scanner.scan_once()

    # Second scan: fee_rate=50 → FEE_RATE_CHANGED mutation
    mutation_calls: list[tuple] = []

    async def on_mut(token_id: str, mutations: list[MutationType]) -> None:
        mutation_calls.append((token_id, mutations))

    scanner.on_mutation = on_mut
    scanner.http_client = make_http_client([page])
    scanner.fee_cache = make_fee_cache(rate=50)
    await scanner.scan_once()

    assert len(mutation_calls) == 1
    token_id, mutations = mutation_calls[0]
    assert token_id == "tok1"
    assert MutationType.FEE_RATE_CHANGED in mutations


@pytest.mark.asyncio
async def test_mutation_callback_not_fired_when_no_change():
    """Identical data between scans → on_mutation is never called."""
    page = [make_raw_market("tok1")]
    fc = make_fee_cache(rate=0)

    mutation_calls = []
    scanner = UniverseScanner(
        http_client=make_http_client([page]),
        fee_cache=fc,
        on_mutation=AsyncMock(side_effect=lambda *a: mutation_calls.append(a)),
    )
    await scanner.scan_once()
    # Second scan with same data
    scanner.http_client = make_http_client([page])
    await scanner.scan_once()

    assert mutation_calls == []


@pytest.mark.asyncio
async def test_mutation_callback_not_called_when_none():
    """scan_once succeeds silently when on_mutation is None."""
    page = [make_raw_market("tok1")]
    fc = make_fee_cache(rate=0)
    scanner = UniverseScanner(http_client=make_http_client([page]), fee_cache=fc)
    await scanner.scan_once()
    scanner.http_client = make_http_client([page])
    scanner.fee_cache = make_fee_cache(rate=99)  # mutation, but no callback set
    await scanner.scan_once()   # must not raise


# ── Resolution watchlist (FR-505) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watchlist_no_new_entries_within_2h():
    """Market 90 minutes from resolution → no_new_entries=True."""
    now = datetime.now(tz=timezone.utc)
    res_time = (now + timedelta(minutes=90)).isoformat()
    page = [make_raw_market("tok1", resolution_time=res_time)]

    scanner = UniverseScanner(http_client=make_http_client([page]), fee_cache=make_fee_cache())
    await scanner.scan_once()

    entry = scanner.watchlist()
    assert len(entry) == 1
    assert entry[0].no_new_entries is True
    assert entry[0].force_cancel is False


@pytest.mark.asyncio
async def test_watchlist_force_cancel_within_30min():
    """Market 20 minutes from resolution → force_cancel=True."""
    now = datetime.now(tz=timezone.utc)
    res_time = (now + timedelta(minutes=20)).isoformat()
    page = [make_raw_market("tok1", resolution_time=res_time)]

    scanner = UniverseScanner(http_client=make_http_client([page]), fee_cache=make_fee_cache())
    await scanner.scan_once()

    entry = scanner.watchlist()
    assert len(entry) == 1
    assert entry[0].force_cancel is True
    assert entry[0].no_new_entries is True   # force implies warn too


@pytest.mark.asyncio
async def test_watchlist_not_added_when_far_from_resolution():
    """Market 5 hours from resolution → not in watchlist."""
    now = datetime.now(tz=timezone.utc)
    res_time = (now + timedelta(hours=5)).isoformat()
    page = [make_raw_market("tok1", resolution_time=res_time)]

    scanner = UniverseScanner(http_client=make_http_client([page]), fee_cache=make_fee_cache())
    await scanner.scan_once()

    assert scanner.watchlist() == []


@pytest.mark.asyncio
async def test_watchlist_not_added_when_resolution_time_none():
    """Market with no resolution_time is never in the watchlist."""
    page = [make_raw_market("tok1")]   # no resolutionTime
    scanner = UniverseScanner(http_client=make_http_client([page]), fee_cache=make_fee_cache())
    await scanner.scan_once()
    assert scanner.watchlist() == []


def test_is_within_warn_window_true():
    scanner = UniverseScanner(http_client=AsyncMock(), fee_cache=MagicMock())
    now = datetime.now(tz=timezone.utc)
    scanner._watchlist["tok1"] = ResolutionWatchlistEntry(
        token_id="tok1",
        condition_id="c1",
        resolution_time=now + timedelta(minutes=60),
        no_new_entries=True,
        force_cancel=False,
    )
    assert scanner.is_within_warn_window("tok1") is True
    assert scanner.is_within_warn_window("tok_other") is False


def test_is_within_pull_window_true():
    scanner = UniverseScanner(http_client=AsyncMock(), fee_cache=MagicMock())
    now = datetime.now(tz=timezone.utc)
    scanner._watchlist["tok1"] = ResolutionWatchlistEntry(
        token_id="tok1",
        condition_id="c1",
        resolution_time=now + timedelta(minutes=15),
        no_new_entries=True,
        force_cancel=True,
    )
    assert scanner.is_within_pull_window("tok1") is True
    assert scanner.is_within_pull_window("unknown") is False


# ── HTTP error handling ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_once_http_error_returns_empty():
    """HTTP failure on first page returns empty list (does not raise)."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("connection refused"))
    scanner = UniverseScanner(http_client=client, fee_cache=make_fee_cache())
    markets = await scanner.scan_once()
    assert markets == []


# ── stop() ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_sets_running_false():
    scanner = UniverseScanner(http_client=AsyncMock(), fee_cache=MagicMock())
    scanner._running = True
    await scanner.stop()
    assert scanner._running is False
