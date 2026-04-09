# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Production-grade automated trading bot for Polymarket (prediction markets). Implements three concurrent strategies:
- **Strategy A**: Event-driven market making (primary revenue driver; zero maker fees + USDC rebates)
- **Strategy B**: Penny option scooping ($0.001–$0.03 shares, 100x–500x tail returns)
- **Strategy C**: Resolution-window sniping (4-to-2-hour pre-resolution GTD bids on high-certainty markets)

## Commands

```bash
# Install dependencies (Poetry)
poetry install

# Run all tests
pytest -x

# Run a single test file
pytest tests/unit/test_settings.py -v

# Run tests with coverage
pytest --cov=. tests/

# Lint and type check
ruff check .
mypy .

# Start the bot (DRY_RUN=true by default)
python -m bot

# Apply DB migrations
python -m scripts.migrate
```

`DRY_RUN=true` must be set throughout development (Steps 0–14 of the implementation plan). Only Steps 14–15 touch the live API.

## Architecture

Three-plane separation of concerns:

**Execution Plane** (`core/execution/`) — latency-sensitive hot path:
- `market_stream.py` — `MarketStreamGateway`: public Market WebSocket → `BookEvent` queue
- `user_stream.py` — `UserStreamGateway`: authenticated User WebSocket → fill/cancel/ack queues
- `book_state.py` — `BookStateStore`: in-memory local order book per token (not historical)
- `quote_engine.py` — computes desired-order state from live book, fees, rewards, inventory
- `execution_actor.py` — diffs desired vs. confirmed state → minimum cancel/place mutations
- `liveness.py` — three independent asyncio tasks: order-safety heartbeat (5s), WS keepalive (10s PING/PONG), sports WS heartbeat (conditional)
- `risk_gate.py` — synchronous hard checks before any placement leaves the process

**Control Plane** (`core/control/`) — slower discovery/analytics:
- `capability_enricher.py` — single point mapping raw API responses (mixed casing) → `MarketCapabilityModel`; the ONLY module that touches raw API field names
- `universe_scanner.py` — Gamma API full-catalog discovery every 5 minutes
- `market_ranker.py` — EV model: `spread_EV + reward_EV + rebate_EV - adverse_selection_cost - inventory_cost - event_risk_cost`
- `sports_adapter.py` — isolates sports-specific behavior (auto-cancel at game start, GTD before game start, 3s marketable delay)
- `parameter_service.py` — live config distribution without code changes

**Ledger Plane** (`core/ledger/`):
- `order_ledger.py`, `fill_position_ledger.py`, `reward_rebate_ledger.py`, `recovery_coordinator.py`
- Storage: Redis (operational cache — acceptable to lose) + Postgres (durable ledger, append-only)
- Fallback: encrypted JSON file for minimal/prototype deployments only

**Other top-level modules:** `config/`, `auth/`, `fees/`, `inventory/`, `strategies/`, `storage/`, `alerts/`, `metrics/`

### Internal Order State Model

Three states per token maintained simultaneously:
- **Desired Orders** — what the Quote Engine wants resting right now
- **Live Orders** — what the system currently believes is live on the CLOB
- **Confirmed Orders** — acknowledged via User WebSocket + reconciliation APIs

The Execution Actor diffs Desired → Confirmed; never Desired → Live. On reconnect, Confirmed state is rebuilt via open-orders query before Desired state is evaluated.

### Event Flows

```
Market Discovery:  Universe Scanner → Capability Enricher → Market Ranker → Market Stream Gateway
Quote Update:      Market WS event → Book State Store → Quote Engine → Order Diff → Risk Gate → Execution Actor
Fill/Reconcile:    User WS event → Fill & Position Ledger → Inventory Update → Quote Engine
Liveness Recovery: Heartbeat failure → Liveness Manager → Cancel/Quarantine → Recovery Coordinator → Resume
```

## Critical Implementation Rules

### API Field Naming (P2 — causes KeyError bugs)
- **Gamma API** → camelCase: `acceptingOrders`, `secondsDelay`, `gameStartTime`, `negRisk`, `tickSize`, `minimumOrderSize`, `resolutionTime`
- **CLOB order book** → snake_case: `tick_size`, `neg_risk`, `min_order_size`
- **EIP-712 payloads** → camelCase: `tokenID`, `price`, `size`, `feeRateBps`, `negRisk`, `tickSize`
- **Internal models** → snake_case throughout
- The `CapabilityEnricher` is the **only** module that maps raw API fields to internal model fields

### Fee Engine Rules
- Never hardcode fee rates or fee-eligible market categories — always fetch at runtime via `/fee-rate/{token_id}`
- `fee_rate_bps / 10000` before use in any spread formula (e.g., `78 bps → 0.0078`)
- Every EIP-712 signed order on a fee-eligible market **must** include `feeRateBps` — missing it is a fatal rejection
- Fee cache TTL: 30s. Re-fetch immediately on fill events. Invalidate if new value deviates >10% from cached
- Strategy A entry requires: `min_spread = max(BASE_SPREAD, 2 × fee_rate_decimal + COST_FLOOR)` AND observed spread ≥ 3× fee rate when `fee_rate_bps > 100`
- Strategy C entry requires: `fee_rate_bps < SNIPE_MAX_FEE_BPS` (hard gate, not ≤)

### Order Type Policy (P3)
- **GTD** for all time-bounded exposure: pre-resolution window, pre-game-start, Strategy C. Expiry: `target_cutoff_unix - buffer_seconds + 60` (the +60 accounts for platform's 1-minute security threshold)
- **GTC** for open-horizon markets only
- **Post-Only** for all Strategy A and C orders (never pay taker fees)
- **FOK/FAK** reserved for inventory rebalancing only

### Sub-100ms Hot Path
REST polling is **prohibited** in Strategy A and C execution paths. Cancel-before-place fires in immediate succession (fire-and-forget on cancel). Retry sequence on duplicate-ID rejection: 10ms → 25ms → 50ms → force reconciliation.

### Heartbeat (Production-Critical)
The order-safety heartbeat (POST to CLOB every 5s) is separate from the WS keepalive. The platform cancels **all open orders** if no heartbeat is received within 10 seconds. These are independent asyncio tasks; failure in one must not affect the others.

### WebSocket Auth
User channel auth is sent in the **subscription message body**, not as HTTP headers. Send after connection is established.

### Sports Markets
All markets with non-null `game_start_time` must go through the Sports Market Adapter. Open quotes are auto-cancelled at game start by the platform. Do not share execution logic with non-sports markets.

## Technology Stack

| Component | Version |
|-----------|---------|
| Python | 3.11+ |
| Concurrency | asyncio + uvloop (Linux/macOS only; default asyncio on Windows) |
| py-clob-client | 0.20.0 (pinned — breaking changes without announcement) |
| web3.py | 6.14.0 (pinned — later versions break Polygon CTF approval flow) |
| eth-account | 0.11.2 |
| websockets | 12.0 |
| Redis | operational cache |
| Postgres (asyncpg 0.29.0) | durable ledger |
| Blockchain | Polygon PoS, Chain ID 137, USDC.e |

**Wallet:** Type 2 (Gnosis Safe via Builder Relayer, gasless) for production; Type 0 (EOA) for local dev and Relayer failover. EOA failover activates after `EOA_FALLBACK_TIMEOUT_S` of Relayer unreachability.

## Configuration

All settings are Pydantic `BaseSettings` in `config/settings.py`. Required fields with no default: `PRIVATE_KEY`, `POLYGON_RPC_URL`, `BUILDER_API_KEY`, `BUILDER_SECRET`, `BUILDER_PASSPHRASE`. `DRY_RUN=True` by default.

Strategy enable/disable flags (`STRATEGY_A_ENABLED`, `STRATEGY_B_ENABLED`, `STRATEGY_C_ENABLED`) and `STRATEGY_A_UNIVERSE_TAGS` support live phasing without code changes.

## Testing

- Framework: pytest + pytest-asyncio
- `tests/conftest.py` provides: `settings` fixture (`DRY_RUN=True`), `mock_clob_client` (`AsyncMock`), `mock_ws_message()` factory
- Acceptance gate for each implementation step: `pytest -x` on the specified test files
- Integration tests in `tests/integration/` require real Redis/Postgres; unit tests must not

## Deployment Phasing (v1)

Per PRD recommendation: pick ONE market environment (crypto with RTDS or sports/esports), enable only Strategy A for the first 14 live days, validate the markout gate (30-second post-fill mid-price delta), then enable B and C. The markout gate — not P&L targets — is the readiness signal.
