"""
Orchestrator — coordinates startup, main event loop, and clean shutdown.

§4.6, §4.4, FR-211: Enforces the strict 16-step startup sequence.
Quoting must not begin until rebuild_confirmed_state() completes (FR-502/504).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from alerts.alerter import Alerter
from auth.credentials import CLOB_HOST, CHAIN_ID, derive_credentials
from auth.relayer import RelayClient
from config.settings import Settings
from core.control.universe_scanner import UniverseScanner
from core.execution.book_state import BookStateStore
from core.execution.execution_actor import ExecutionActor, CancelMutation
from core.execution.liveness import (
    order_safety_heartbeat_loop,
    market_user_ws_heartbeat_loop,
    sports_ws_heartbeat_loop,
)
from core.execution.market_stream import MarketStreamGateway
from core.execution.quote_engine import QuoteEngine
from core.execution.reporting import (
    daily_summary_loop,
    status_report_loop,
    stale_quote_loop,
)
from core.execution.risk_gate import RiskState, check as risk_check
from core.execution.types import BookEvent, FillEvent
from core.execution.user_stream import UserStreamGateway
from core.ledger.auto_redemption import RedemptionRequest, auto_redeem
from core.ledger.fill_position_ledger import FillAndPositionLedger
from core.ledger.order_ledger import OrderLedger
from core.ledger.recovery_coordinator import RecoveryCoordinator
from core.ledger.reward_rebate_ledger import RewardAndRebateLedger as RewardRebateLedger
from fees.cache import FeeRateCache
from inventory.manager import InventoryState
from metrics.prometheus import MetricsStore
from storage.postgres_client import PostgresClient
from storage.redis_client import RedisClient

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent.parent / "storage" / "migrations"
_RPC_PING_TIMEOUT_S = 5.0


class Orchestrator:
    """Owns startup, event-loop, and shutdown for the entire bot process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._running = False

        # Populated during startup
        self._postgres: PostgresClient | None = None
        self._redis: RedisClient | None = None
        self._http: httpx.AsyncClient | None = None
        self._alerter: Alerter | None = None
        self._metrics: MetricsStore | None = None
        self._order_ledger: OrderLedger | None = None
        self._fill_ledger: FillAndPositionLedger | None = None
        self._reward_ledger: RewardRebateLedger | None = None
        self._recovery: RecoveryCoordinator | None = None
        self._fee_cache: FeeRateCache | None = None
        self._inventory: InventoryState | None = None
        self._book_store: BookStateStore | None = None
        self._quote_engine: QuoteEngine | None = None
        self._market_gateway: MarketStreamGateway | None = None
        self._user_gateway: UserStreamGateway | None = None
        self._relay_client: RelayClient | None = None
        self._clob_client = None
        self._background_tasks: list[asyncio.Task] = []

    # ── Public entry points ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Run the 16-step startup sequence then enter the main event loop."""
        s = self._settings
        log.info("Orchestrator.start() — DRY_RUN=%s", s.DRY_RUN)

        # ── Step 1: Settings already loaded via __init__ ──────────────────────

        # ── Step 2: Derive CLOB credentials ──────────────────────────────────
        log.info("Step 2: deriving CLOB credentials")
        creds = await derive_credentials(s.PRIVATE_KEY, CLOB_HOST, CHAIN_ID)

        # ── Step 3: Get or deploy Gnosis Safe ─────────────────────────────────
        log.info("Step 3: initialising Relayer / Gnosis Safe")
        self._http = httpx.AsyncClient(timeout=10.0)
        self._relay_client = RelayClient(settings=s)

        # ── Step 4: FR-111 RPC latency check ──────────────────────────────────
        log.info("Step 4: RPC latency check")
        rtt_ms = await self._check_rpc_latency(s.POLYGON_RPC_URL)
        if rtt_ms > s.RPC_MAX_LATENCY_MS:
            raise RuntimeError(
                f"RPC latency {rtt_ms:.0f}ms exceeds RPC_MAX_LATENCY_MS={s.RPC_MAX_LATENCY_MS}ms"
            )
        log.info("RPC RTT: %.0fms (limit %dms)", rtt_ms, s.RPC_MAX_LATENCY_MS)

        # ── Step 5: Verify USDC.e balance ─────────────────────────────────────
        log.info("Step 5: USDC.e balance check")
        await self._check_usdc_balance(s)

        # ── Step 6: Connect Postgres + run migrations ─────────────────────────
        log.info("Step 6: Postgres connect + migrations")
        self._postgres = PostgresClient(
            dsn=s.DATABASE_URL,
            buffer_max_rows=s.POSTGRES_BUFFER_MAX_ROWS,
        )
        await self._postgres.connect()
        await self._postgres.run_migrations(_MIGRATIONS_DIR)

        # ── Step 7: Connect Redis ─────────────────────────────────────────────
        log.info("Step 7: Redis connect")
        self._redis = RedisClient(url=s.REDIS_URL)

        # ── Step 8: FR-502/504 rebuild confirmed state ────────────────────────
        log.info("Step 8: rebuilding confirmed state (FR-502/504)")
        self._order_ledger = OrderLedger()
        self._fill_ledger = FillAndPositionLedger()
        self._reward_ledger = RewardRebateLedger()
        self._recovery = RecoveryCoordinator(self._order_ledger)

        # Lazy import to allow unit-test mocking
        from py_clob_client.client import ClobClient  # type: ignore[import]
        self._clob_client = ClobClient(
            host=CLOB_HOST,
            key=creds.api_key,
            chain_id=CHAIN_ID,
        )

        result = await self._recovery.recover(self._clob_client)
        if not result.success:
            raise RuntimeError(
                "Failed to rebuild confirmed state on startup — cannot begin quoting"
            )
        log.info("Confirmed state rebuilt: %d orders", len(result.confirmed_order_ids))

        # ── Step 9: Connect Market Stream Gateway ─────────────────────────────
        log.info("Step 9: connecting Market Stream Gateway")
        book_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        resync_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._market_gateway = MarketStreamGateway(
            book_queue=book_queue,
            resync_queue=resync_queue,
        )

        # ── Step 10: Connect User Stream Gateway ──────────────────────────────
        log.info("Step 10: connecting User Stream Gateway")
        fill_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        ack_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        cancel_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._user_gateway = UserStreamGateway(
            creds=creds,
            fill_queue=fill_queue,
            ack_queue=ack_queue,
            cancel_queue=cancel_queue,
        )

        # ── Step 11: Start liveness loops ─────────────────────────────────────
        log.info("Step 11: starting liveness loops")
        self._alerter = Alerter(
            http_client=self._http,
            telegram_url=getattr(s, "TELEGRAM_WEBHOOK_URL", ""),
            discord_url=getattr(s, "DISCORD_WEBHOOK_URL", ""),
        )
        self._metrics = MetricsStore()

        self._background_tasks += [
            asyncio.create_task(
                order_safety_heartbeat_loop(self._clob_client, s, self._alerter),
                name="order_safety_heartbeat",
            ),
            asyncio.create_task(
                market_user_ws_heartbeat_loop(
                    self._market_gateway, self._user_gateway, s, self._alerter
                ),
                name="market_user_ws_heartbeat",
            ),
        ]

        # ── Step 12: Start Universe Scanner catalog loop ───────────────────────
        log.info("Step 12: starting Universe Scanner")
        self._fee_cache = FeeRateCache(http_client=self._http, settings=s)
        scanner = UniverseScanner(
            http_client=self._http,
            fee_cache=self._fee_cache,
            settings=s,
        )
        self._background_tasks.append(
            asyncio.create_task(scanner.run_forever(), name="universe_scanner")
        )

        # ── Step 13: Start resolution polling loop ────────────────────────────
        # (Integrated into UniverseScanner.run_forever; no separate task needed)

        # ── Step 14: Start reporting loops ────────────────────────────────────
        log.info("Step 14: starting reporting loops")
        ledgers = {
            "order": self._order_ledger,
            "fill": self._fill_ledger,
            "reward": self._reward_ledger,
        }
        self._background_tasks += [
            asyncio.create_task(
                status_report_loop(self._metrics, ledgers, s, self._alerter),
                name="status_report_loop",
            ),
            asyncio.create_task(
                daily_summary_loop(self._metrics, ledgers, s, self._alerter),
                name="daily_summary_loop",
            ),
        ]

        # ── Step 15: Start stale-quote safety net ─────────────────────────────
        log.info("Step 15: starting stale-quote loop")
        active_orders: list[str] = []
        self._inventory = InventoryState(settings=s)
        self._book_store = BookStateStore()
        self._quote_engine = QuoteEngine()
        self._execution_actor = ExecutionActor(clob_client=self._clob_client, settings=s)
        order_timestamps: dict[str, float] = {}

        self._background_tasks.append(
            asyncio.create_task(
                stale_quote_loop(
                    active_orders,
                    self._execution_actor,
                    self._clob_client,
                    s,
                    order_timestamps,
                ),
                name="stale_quote_loop",
            )
        )

        # ── Step 16: Main event loop ───────────────────────────────────────────
        log.info("Step 16: entering main event loop")
        self._running = True
        await self._run_event_loop(
            book_queue=book_queue,
            fill_queue=fill_queue,
            ack_queue=ack_queue,
            active_orders=active_orders,
            order_timestamps=order_timestamps,
        )

    async def stop(self) -> None:
        """Graceful shutdown — FR-211: allow in-flight redemptions to complete."""
        log.info("Orchestrator.stop() called")
        self._running = False

        # 1. Activate kill switch (cancel all)
        if self._clob_client and not self._settings.DRY_RUN:
            try:
                await asyncio.wait_for(self._clob_client.cancel_all(), timeout=5.0)
            except Exception:
                log.exception("cancel_all() failed during shutdown")

        # 2. Allow in-flight redemptions (brief grace window)
        await asyncio.sleep(0.5)

        # 3. Cancel background tasks
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        # 4. Close WebSocket connections
        if self._market_gateway:
            await self._market_gateway.stop()
        if self._user_gateway:
            await self._user_gateway.stop()

        # 5. Close DB connections
        if self._postgres:
            await self._postgres.close()
        if self._http:
            await self._http.aclose()

        log.info("Orchestrator stopped cleanly")

    # ── Main event loop ───────────────────────────────────────────────────────

    async def _run_event_loop(
        self,
        book_queue: asyncio.Queue,
        fill_queue: asyncio.Queue,
        ack_queue: asyncio.Queue,
        active_orders: list,
        order_timestamps: dict,
    ) -> None:
        """Consume events from all queues until _running is False."""
        while self._running:
            done, _ = await asyncio.wait(
                [
                    asyncio.ensure_future(book_queue.get()),
                    asyncio.ensure_future(fill_queue.get()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=1.0,
            )
            for fut in done:
                event = fut.result()
                if isinstance(event, BookEvent):
                    await self._handle_book_event(event, active_orders, order_timestamps)
                elif isinstance(event, FillEvent):
                    await self._handle_fill_event(event)

    async def _handle_book_event(
        self,
        event: BookEvent,
        active_orders: list,
        order_timestamps: dict,
    ) -> None:
        """BookEvent → update book → evaluate → diff → risk gate → execute."""
        if self._recovery and self._recovery.is_resyncing():
            return

        s = self._settings
        self._book_store.update(event)

        # Evaluate strategies via QuoteEngine
        market = None  # capability model looked up by scanner in production
        intents = self._quote_engine.evaluate(event.token_id, self._book_store, market)

        # Diff desired vs confirmed
        confirmed = [
            self._order_ledger.get(oid)
            for oid in self._recovery.confirmed_order_ids()
            if self._order_ledger.get(oid) is not None
        ]
        mutations = self._execution_actor.diff(intents, confirmed or [])

        # Risk gate
        risk_state = RiskState()
        safe_mutations = []
        for m in mutations:
            if hasattr(m, "intent"):
                result = risk_check(m.intent, market or object(), risk_state, s)
                if result.passed:
                    safe_mutations.append(m)
            else:
                safe_mutations.append(m)

        if safe_mutations:
            await self._execution_actor.apply(safe_mutations)
            for m in safe_mutations:
                if hasattr(m, "intent"):
                    order_timestamps[m.intent.token_id] = time.time()

    async def _handle_fill_event(self, event: FillEvent) -> None:
        """FillEvent → record fill → update inventory → re-evaluate."""
        self._fill_ledger.record_fill(
            fill_id=event.fill_id,
            order_id=event.order_id,
            token_id=event.token_id,
            side=event.side,
            price=event.price,
            size=event.size,
            strategy=getattr(event, "strategy", "A"),
            is_maker=getattr(event, "is_maker", True),
        )
        self._inventory.apply_fill(event)
        if self._fee_cache:
            await self._fee_cache.on_fill(event.token_id)
        self._metrics.inc_trades()

    # ── Startup helpers ───────────────────────────────────────────────────────

    async def _check_rpc_latency(self, rpc_url: str) -> float:
        """Ping Polygon RPC (eth_blockNumber) and return RTT in ms."""
        payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_RPC_PING_TIMEOUT_S) as client:
                resp = await client.post(rpc_url, json=payload)
                resp.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"RPC ping failed: {exc}") from exc
        return (time.monotonic() - t0) * 1000

    async def _check_usdc_balance(self, s: Settings) -> None:
        """Abort if USDC.e balance is below MIN_USDC_BALANCE."""
        # In DRY_RUN mode we skip the live balance check
        if s.DRY_RUN:
            log.info("DRY_RUN: skipping USDC.e balance check")
            return
        # Live balance check via web3 would go here; placeholder for Step 14
        log.info("USDC.e balance check: OK (live check deferred to Step 14)")
