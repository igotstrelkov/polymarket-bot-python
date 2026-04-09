# Product Requirements Document
## Polymarket Automated Trading Bot — v3.14

**Version:** 3.14
**Date:** March 26, 2026
**Status:** Final
**Classification:** Internal / Confidential
**Supersedes:** v2.2, v2.1, v2.0, v1.0

---

> **JURISDICTIONAL PREREQUISITE:** Polymarket's Terms of Service prohibit US persons and persons from certain other restricted jurisdictions from trading via both the UI and API, including agents developed by persons in restricted jurisdictions. The operator must verify their eligibility before deploying this system. This PRD does not constitute legal advice.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Overview](#2-product-overview)
3. [Platform Context and Fee Regime](#3-platform-context-and-fee-regime)
4. [System Architecture](#4-system-architecture)
5. [Trading Strategies](#5-trading-strategies)
6. [Functional Requirements](#6-functional-requirements)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [API Integration Specification](#8-api-integration-specification)
9. [Configuration Parameters](#9-configuration-parameters)
10. [Development Milestones](#10-development-milestones)
11. [Testing Strategy](#11-testing-strategy)
12. [Risks and Mitigations](#12-risks-and-mitigations)
13. [Success Metrics](#13-success-metrics)
14. [v4 Roadmap](#14-v4-roadmap)
15. [Glossary](#15-glossary)
16. [Document History](#16-document-history)

---

## 1. Executive Summary

This PRD defines the requirements for a production-grade automated trading bot for Polymarket. It is the result of merging two architectural generations: the v2.x series, which contributed detailed implementation specifics for the current fee/signing surface and a sub-100ms execution constraint, and a new architectural reference, which contributed a cleaner three-plane separation of concerns, runtime capability discovery, a formal order-state model, and a sports market adapter concept.

The system architecture supports three concurrent strategies: event-driven spread-capture market making (Strategy A), penny option scooping (Strategy B), and resolution-window sniping (Strategy C). It is implemented in Python using asyncio and the official `py-clob-client` SDK.

The central architectural principle is that **the execution engine must react to live market capabilities derived at runtime, not to hard-coded category assumptions**. Fees, tick sizes, reward configurations, and market-specific behaviors are fetched and cached dynamically. The system never assumes a fee rate, a reward structure, or a behavioral rule without reading it from the current API surface.

### v1 Deployment Scope Recommendation

**The architecture in this PRD supports all three strategies across all market types, but the first live deployment should target one environment, not all of them simultaneously.** Running three strategies across crypto and sports/esports in the first deployment is too much operational surface to validate at once — adverse selection patterns, fee behavior, and fill quality are meaningfully different across environments, and mixing them makes it harder to diagnose what is and isn't working.

Two attractive v1 environments exist as of April 2026:

**Option A — Crypto markets with RTDS:** Polymarket's Real-Time Data Sources give you first-party Binance and Chainlink price feeds plus dynamic subscriptions on crypto markets. The fee regime is well-documented here, and the RTDS integration gives a structural information advantage for quote positioning. This is the more technically demanding option but has a clearer edge.

**Option B — Sports/esports markets:** Liquidity incentives are currently concentrated here in April 2026. The sports adapter is already specified (FR-119), and the simpler information environment (no real-time price feed to integrate) makes the operational validation easier.

**Recommended sequence:**
1. Pick one environment for v1 live deployment. Enable only Strategy A (`STRATEGY_B_ENABLED=false`, `STRATEGY_C_ENABLED=false`) for the first 14 live days.
2. Pass the markout gate (§11.4 Step 3) for Strategy A in that environment before enabling Strategy C.
3. Enable Strategy B and Strategy C in v1.1 once Strategy A markout data is clean.
4. Expand to the second environment in v1.2.

The `STRATEGY_*_ENABLED` flags and `STRATEGY_A_UNIVERSE_TAGS` config parameter allow this phasing without code changes.

### Deferred to v4

- **NegRisk multi-outcome arbitrage** — requires N-leg FOK execution and NegRisk Adapter interaction
- **Toxic flow protection** — needs 30+ days of live fill data to calibrate
- **Cross-platform arbitrage** — Polymarket vs. Kalshi; research phase required
- **Grafana dashboard** — Prometheus export is v3 scope; dashboard layer is v4
- **Cross-market correlated arbitrage** — research phase required
- **Predictive modeling / AI outcome forecasting**

---

## 2. Product Overview

### 2.1 Strategic Philosophy

The bot executes a **maker-first barbell strategy** combining stable, fee-free liquidity provision with high-reward outlier bets.

**Strategy A (market making)** is the stable income core. Makers pay zero fees and earn USDC rebates funded by taker fees. Market making on Polymarket now requires runtime awareness of fee rates, reward configurations, and order scoring status — none of these can be hardcoded safely given the evolving fee regime.

**Strategy B (penny scooping)** is the convex end of the barbell. Most positions expire worthless, but rare hits deliver 100x–500x returns. This strategy is uncorrelated with Strategy A and unaffected by fee changes.

**Strategy C (resolution sniping)** exploits a recurring mispricing as markets approach resolution with high certainty. The bot places Post-Only GTD bids at a slight discount to fair value in the 4-to-2-hour pre-resolution window, with expiry set to `resolutionTime - GTD_RESOLUTION_BUFFER_MS // 1000 + 60`. GTD is correct here — the 4-to-2-hour pre-resolution window is exactly the time-bounded exposure scenario Design Principle P3 specifies GTD for. GTC would leave orders resting indefinitely if the market extends. Entry is restricted to markets where the dynamic fee is near-zero (probability > 0.90 or < 0.10).

### 2.2 Vision Statement

Build a self-sustaining automated trading system that generates consistent risk-adjusted returns by harvesting structural pricing inefficiencies on Polymarket, operating as a preferred liquidity provider, and maintaining the operational discipline required to avoid adverse selection — with system behavior driven entirely by current market capabilities read from the API at runtime.

### 2.3 Target Performance Benchmarks

The financial targets below are **business aspirations for a fully ramped, multi-strategy deployment — not operational validation gates for early-stage deployment.** The primary readiness signal for Strategy A is the live markout gate (§11.4 Step 3), not these P&L figures. The markout gate focuses on fill toxicity — the real question for a market-making strategy — which simulated P&L cannot answer. Do not use P&L against these targets to decide whether to scale capital; use the markout gate.

| Metric | Aspiration | Notes |
|--------|--------|-------|
| Annual net profit | $70,000 – $110,000 | Aspirational for full ramp; not an early-deployment gate |
| Daily net profit | > $192/day average | $70,000 ÷ 365 = $191.78; aspirational for full ramp |
| Daily trade volume | 100 – 180 trades | |
| Max single-trade profit share | < 3% of total P&L | Diversification discipline |
| Max drawdown | < 5% of capital | Kill switch enforced |
| Uptime | 99.5%+ | |
| Gas cost | $0/day | Gasless via Gnosis Safe and Builder Relayer |
| Cancel/replace latency | < 100ms P95 desk target | See §3.2 for rationale and caveats |
| Maker-to-taker ratio (A+C) | > 98% | Strategy B uses taker orders by design |

### 2.4 Revenue Streams

| Source | Type | Description |
|--------|------|-------------|
| Spread capture | Trading | Bid-ask spread earned on filled maker quotes. Primary revenue driver. |
| Maker rebates | Platform incentive | Daily USDC rebates funded by taker fees, proportional to executed maker volume on eligible markets. |
| Penny option payoffs | Trading | Rare 100x–500x returns from tail-risk positions that resolve favorably. |
| Resolution-window capture | Trading | Maker bids on near-certainty outcomes (YES > 90% or NO > 90%) in the 4-to-2-hour pre-resolution window. |
| Liquidity rewards | Platform incentive | Daily USDC for maintaining qualifying quotes within published spread and size parameters. |

### 2.5 Scope Boundaries

**In Scope:**
- Three concurrent trading strategies: event-driven market making, penny option scooping, resolution-window sniping
- Sub-100ms event-driven cancel/replace loop driven by WebSocket events; REST polling prohibited in hot path
- Runtime capability discovery: fee rates, tick sizes, reward configurations, `accepting_orders`, `seconds_delay`, negRisk flags all fetched and cached dynamically
- Full-catalog market discovery via Gamma API with 5-minute rescan
- Real-time order book monitoring via WebSocket with CLOB heartbeat management
- Gasless execution via Builder Relayer and Gnosis Safe; EOA failover on Relayer outage
- Active inventory skew management with automatic quote price adjustment and halt/resume
- Auto-redemption of winning positions via CTF `redeemPositions()` contract call
- Liquidity Rewards and Maker Rebates optimization driven by live reward surfaces
- Risk management: per-market limits, daily loss limit, drawdown kill switch, resolution-aware quoting
- Sports market adapter isolating game-start cancellation and marketable-order delay behavior
- Redis operational cache + Postgres ledger for orders, fills, positions, rewards, rebates
- Session heartbeats with reconnection and order state reconciliation using Desired/Live/Confirmed model
- Structured JSON logging with Prometheus metrics export; Telegram/Discord alerting
- Strategy enable/disable flags for live operational control
- Mandatory 24-hour latency shadow run and 14-day P&L paper trading validation

**Out of Scope (v4+):**
- NegRisk multi-outcome arbitrage
- Toxic flow protection and elite wallet monitoring
- Cross-platform arbitrage (Polymarket vs. Kalshi)
- Cross-market correlated arbitrage
- Grafana dashboard
- Predictive modeling or AI-based outcome forecasting
- Multi-user or multi-wallet orchestration
- Regulatory compliance advisory

---

## 3. Platform Context and Fee Regime

Every engineer must read this section before writing any code.

### 3.1 Dynamic Taker Fee Schedule

Polymarket's fee regime is actively evolving. The official docs show fees as a live and expanding schedule — not a static configuration — which is exactly why the system must derive all fee-related behavior at runtime.

**Historical fee rollout snapshot (as of March 26, 2026 — treat as background context only, not as a current operational reference):**

> ⚠️ The table below reflects the fee schedule as documented at the time this PRD was written. Polymarket's fee regime has continued to expand since then — the March 31, 2026 REST update introduced broader `feeSchedule` fields and additional fee-enabled categories beyond those listed here. Do not use this table to determine whether a market is fee-eligible. Always use the runtime-discovery path (Design Principle P2): read `feesEnabled` and `/fee-rate/{token_id}` per market at runtime.

| Market Type | Status as of March 26, 2026 |
|------------|--------|
| 15-min crypto markets | Taker fees live since January 5, 2026 |
| 5-min crypto markets | Taker fees live since February 12, 2026 |
| NCAAB, Serie A | Taker fees live since February 18, 2026 |
| All crypto markets (1H, 4H, daily, weekly) | Taker fees effective March 6, 2026 (new markets only) |

**Fee formula (illustrative only):**

```
fee = C × feeRate × p × (1 - p)
```

where `p` is the YES probability, `feeRate` is the market-specific fee rate, and `C` is a market-specific constant. The fee peaks at `p = 0.50` and approaches zero at extremes. **Makers are never charged fees — only takers pay fees.** This formula is for conceptual understanding only and does not substitute for reading `feeRateBps` from the API at runtime. The constant `C` is not directly queryable.

**Implementation rule:** The system must always obtain the current fee by querying the dedicated `/fee-rate` CLOB endpoint per token, or by allowing the official SDK to handle it automatically. The official SDKs (`py-clob-client`, `@polymarket/clob-client`) fetch the fee rate and include `feeRateBps` in the signed order payload automatically when using their order creation methods. If using custom signing, call `GET /fee-rate?token_id={id}` directly. Hardcoding fee rates or fee-eligible market categories is forbidden.

**EIP-712 requirement:** Every signed order payload submitted to a fee-eligible market must include the `feeRateBps` field. Orders missing this field are rejected outright.

**Bps-to-decimal conversion:** Before use in any spread formula, `fee_rate_bps` must be divided by 10,000 to convert to decimal. Example: `fee_rate_bps = 78` → `fee_rate_decimal = 0.0078`.

### 3.2 Execution Timing and Latency Rationale

The sub-100ms cancel/replace loop requirement is grounded in observable properties of the system, not in any single platform policy statement.

In the Polymarket CLOB, the execution window for a maker quote at a given price is finite. Any taker with a data feed that updates faster than the bot's cancel/replace cycle can fill a stale quote before it is updated. The bot must be designed assuming that no execution delay buffer exists for taker orders — whether or not any historical delay has been present or removed. This is a sound conservative design assumption independent of platform policy history.

**Community context (unverified):** Multiple independent sources from February 2026 report that a 500ms taker execution delay previously relied on by market-making bots was removed without announcement around February 18, 2026. This specific change is not documented in Polymarket's official changelog, which records that date only as the activation date for NCAAB and Serie A taker fees. The community reports are consistent and widely circulated, but cannot be verified from a canonical primary source as of this writing. The architectural response — event-driven quote management with a < 100ms pipeline — is identical regardless of whether this specific change occurred and should not depend on validating the claim.

**Design response:**
- The cancel/replace loop must be event-driven, triggered by WebSocket order book events
- The full pipeline from WebSocket event receipt to new orders dispatched should target < 100ms as a **desk target under normal conditions** — not a stable deployment guarantee. Polymarket's rate limiting uses Cloudflare throttling, and the platform's own error documentation describes throttling, cancel-only mode, exchange pauses, and matching-engine restarts as normal operational conditions that can push response times above any internal target regardless of code quality. The target is meaningful for benchmarking infrastructure and catching code-level regressions; it is not a commitment the platform makes.
- REST polling is prohibited in the Strategy A and C execution paths
- Cancel and place calls are fired in immediate succession (fire-and-forget on cancel); see §5.1.2

### 3.3 The Fee Regime Implication for Strategy

The combination of dynamic taker fees and maker rebates creates a structural advantage for passive market making:

- Makers pay zero fees
- Makers earn USDC rebates funded by taker fees
- Taker strategies near 50% probability require an edge exceeding the market's taker fee rate just to break even — the exact break-even threshold is market-specific and must be read from the `/fee-rate` endpoint at runtime, not assumed from a fixed figure

The winning bot in the current environment is the best liquidity provider, not the fastest arbitrageur.

---

## 4. System Architecture

### 4.1 Architectural Objective

The system is a **metadata-driven market-making platform**, not a monolithic strategy bot. The architecture separates the latency-sensitive order management path from slower discovery, analytics, and reconciliation work. System behavior is derived from current market capabilities read at runtime — fees, tick sizes, reward configurations, and market-specific rules — rather than from static tables in the codebase.

### 4.2 Three-Plane Architecture

The system is split into three functional planes:

**Execution Plane** — latency-sensitive services: market data ingestion, quote calculation, order placement/cancellation, heartbeats, and hard risk checks. Driven by Market and User WebSocket channels and CLOB trading endpoints.

**Control Plane** — slower services: market discovery, capability enrichment, candidate ranking, reward/rebate analysis, parameter management, and strategy configuration. Driven by Gamma API, Data API, rewards endpoints, and public CLOB methods.

**Ledger and Reconciliation Plane** — persistent storage and recovery: orders, fills, positions, rewards, rebates, and restart recovery. Backed by authenticated order/trade endpoints, Data API position/trade endpoints, and durable storage.

### 4.3 Design Principles

**P1. Keep the hot path small.** Only order book updates, quote recalculation, order diffing, order submission/cancellation, and liveness checks belong in the Execution Plane. Market discovery, full-universe rescans, reporting, and parameter optimization must stay off the hot path.

**P2. Use runtime capability discovery.** Before quoting any market, the system shall resolve tick size, fee status and rate, negRisk status, reward configuration, `accepting_orders`, `seconds_delay`, and market-specific behavioral rules from APIs — not from static assumptions. The CLOB `get_order_book()` response exposes `tick_size`, `neg_risk`, and `min_order_size`; fee rate is obtained via the dedicated `/fee-rate` endpoint or SDK auto-handling; rewards are available via dedicated reward endpoints.

**Field naming convention — critical implementation note:** Polymarket's APIs use different casing conventions on different surfaces, and mixing them causes `KeyError` adapter bugs that are easy to introduce and annoying to debug. The following rules apply:

- **Gamma API market objects** use camelCase: `acceptingOrders`, `secondsDelay`, `gameStartTime`, `enableOrderBook`, `negRisk`, `resolutionTime`, `conditionId`, `tickSize`, `minimumOrderSize`
- **CLOB order book endpoint** uses snake_case: `tick_size`, `neg_risk`, `min_order_size`, `last_trade_price`
- **EIP-712 signed order payloads** use camelCase: `tokenID`, `price`, `size`, `side`, `feeRateBps`, `expiration`, `negRisk`, `tickSize`
- **Internal system models** (Python dataclasses/Pydantic) should use snake_case throughout, with explicit field mapping at each API boundary

The Capability Enricher is the single point responsible for mapping raw API responses (with their mixed casing) into the canonical internal `MarketCapabilityModel`. No other module should reference raw API field names — all consumption is through the internal model.

**P3. Prefer passive maker flow.** Polymarket guidance for market makers emphasizes resting limit orders. GTC and GTD are the primary passive tools; FOK and FAK are reserved for inventory rebalancing and explicit taker actions. **GTD is strongly preferred over GTC for time-bounded exposure.** Use GTD aggressively for quotes in markets with known time boundaries (pre-game, pre-resolution, pre-event) so that stale resting orders do not accumulate risk. GTC remains appropriate for markets with no near-term event horizon, where unbounded quote duration is acceptable. Do not replace GTC everywhere with GTD.

**P4. Treat rewards and rebates as first-class signals.** The system shall use reward configs, order scoring status, live user reward percentages, and rebate data to drive market selection and quote sizing — not just reporting.

**P5. Make market-type differences explicit.** The architecture shall support market adapters for special behaviors. Sports markets have documented special behavior: outstanding limit orders are automatically cancelled at game start, and marketable orders carry a 3-second delay. These markets must not share identical execution assumptions with other market types.

### 4.4 Module Breakdown

#### 4.4.1 Execution Plane

| Module | Responsibility |
|--------|----------------|
| Market Stream Gateway | Consumes the public Market WebSocket and normalizes order-book, trade, and lifecycle updates into an internal event stream keyed by token ID. Canonical low-latency market-data source for active subscriptions. |
| User Stream Gateway | Consumes the authenticated User WebSocket and emits normalized fill, cancel, and order-state events. Runs server-side only; the user channel requires API credentials. |
| Book State Store | Maintains in-memory local book, last-trade context, quote version, and live-order view per subscribed token. Updated only from streaming data and explicit reconciliation calls. Not a historical store — an operational state cache. |
| Quote Engine | Computes desired quote set per subscribed token. Inputs: live spread, midpoint, tick size, fee rate, reward eligibility, order scoring state, inventory state, and time-to-event rules. Output: desired-order state (not direct API calls). Market-capability-aware, not category-hardcoded. |
| Order Diff and Execution Actor | Compares desired-order state with confirmed live-order state and emits the minimum mutation set: place, cancel, resize, or replace. Owns all write traffic to the exchange. Uses batch endpoints where available. |
| Liveness Manager | Maintains two mandatory and one conditional liveness loop: **(1) Order-safety heartbeat** — POST heartbeat to CLOB API every 5 seconds; the platform cancels all open orders if a valid heartbeat is not received within 10 seconds (5-second buffer); this is the critical production-safety loop. **(2) Market and User channel WebSocket heartbeat** — the client sends the literal string `PING` as an application-level message every 10 seconds; the server responds with `PONG`; this is separate from the order-safety heartbeat. **(3) Sports channel WebSocket heartbeat (conditional)** — only required if the Sports WebSocket channel is opened; the server sends `ping` every 5 seconds and the client must reply `pong` within 10 seconds; the v3 sports adapter does not require this channel as it derives behavior from `game_start_time` and Gamma market metadata. All active loops must run as independent asyncio tasks. Also manages stream reconnect policy and queries the server-time endpoint to detect local clock drift. |
| Risk Gate | Performs synchronous hard checks before any placement leaves the process: portfolio exposure, per-market exposure, inventory skew, market validity, session health, and `accepting_orders` status. |

#### 4.4.2 Control Plane

| Module | Responsibility |
|--------|----------------|
| Universe Scanner | Discovers active events and markets via Gamma API, paginates the universe, and creates a normalized candidate set. Wide-universe discovery, not low-latency execution. Also runs the resolution confirmation polling loop at `REDEMPTION_POLL_INTERVAL_S` intervals. |
| Capability Enricher | For each candidate token/market, resolves: `feesEnabled`, fee rate, tick size, negRisk status, reward configuration, order scoring viability, `seconds_delay`, and market-type rules. Unifies all per-market capabilities into a single internal market-capability model. |
| Market Ranker | Ranks candidates for quoting using an expected-value model: `maker_EV = spread_EV + reward_EV + rebate_EV - adverse_selection_cost - inventory_cost - event_risk_cost`. Chooses which markets deserve subscriptions and budget; does not manage per-tick quoting. All cost terms must be estimated from data rather than left as conceptual placeholders. The v1 estimation approach for each term: **`spread_EV`** = observed half-spread × estimated fill probability (fill probability proxied from historical fill rate at posted distance from mid). **`reward_EV`** = `rewardsDailyRate` × estimated share of pool based on current quote proximity score. **`rebate_EV`** = per-market rebate rate × expected daily maker volume. **`adverse_selection_cost`** = the hardest term and the one most likely to dominate; estimated in v1 as the average fill-to-mid slippage measured over a 30-second post-fill horizon — i.e., how much the mid moves against the filled side after each fill. This is computed from the Fill and Position Ledger's fill history per market. Markets with consistently negative markout (mid moves >1 tick against the fill direction within 30 seconds of more than X% of fills) are penalised in ranking. **`inventory_cost`** = value-weighted skew × per-tick rebalancing cost estimate. **`event_risk_cost`** = time-to-resolution decay factor applied to any market within 6 hours of resolution. The Analytics Service shall recompute per-market EV inputs weekly from ledger data and publish updated parameters to the Parameter Service. **Cold-start behavior (before ≥ 100 fills or ≥ 24 hours of live data per market):** fill probability is approximated using a fixed monotonic function of distance from mid (e.g., linearly decreasing from 0.5 at mid to 0.05 at 5 ticks out); `adverse_selection_cost` is conservatively overestimated using a fixed penalty of 1 tick regardless of observed markout; market ranking falls back primarily to observed spread width, `rewardsDailyRate`, and order book liquidity depth. Cold-start mode exits per-market as soon as the fill threshold is met; mixed mode (some markets data-driven, others cold-start) is normal during ramp. **Capital allocation:** The Market Ranker selects the top N markets by `maker_EV` (bounded by `MM_MAX_MARKETS`) and distributes quoting exposure proportionally to normalised EV scores within that set, subject to `MAX_PER_MARKET` and `MAX_TOTAL_EXPOSURE`. Markets with EV ≤ 0 are excluded regardless of rank. Each selected market receives at least `MM_MIN_ORDER_SIZE` shares per side; remaining budget is allocated pro-rata to EV score. |
| Sports Market Adapter | Isolates sports-specific execution rules: automatic limit order cancellation at `game_start_time`, 3-second delay for marketable orders, and appropriate quote-expiry behavior using GTD bounded before `game_start_time`. Sports markets must not share identical execution logic with other market types. |
| Parameter Service | Stores and distributes configuration: max markets, quote widths, inventory limits, GTD durations, exposure budgets. Supports live tuning without code changes. Versions every config change for postmortems. |
| Analytics Service | Runs slower analysis: fill quality, reward capture analysis, replay/backtests, parameter sweeps. Consumes persisted ledger data and public price/trade history. Never touches the live execution path. |

#### 4.4.3 Ledger and Reconciliation Plane

| Module | Responsibility |
|--------|----------------|
| Order Ledger | Persists every submitted order, cancel request, acknowledgement, and order-state transition. Backed by authenticated orders endpoints. Resolves ambiguity after reconnects or restarts. |
| Fill and Position Ledger | Persists fills, realized P&L, and current/closed positions. Recovery uses both authenticated trade data and the Data API's user positions/trades endpoints. |
| Reward and Rebate Ledger | Persists per-market reward percentages, daily earnings, scoring state snapshots, and maker rebates. Makes reward capture auditable and feeds the Market Ranker with real capture data rather than estimates. |
| Recovery Coordinator | On startup or reconnect, reconstructs live operational state via an open-orders query, authenticated trades, Data API positions, and the latest persisted snapshots before re-enabling quoting. |

### 4.5 Internal Order State Model

The system maintains three distinct order states per token:

- **Desired Orders** — what the Quote Engine wants resting right now
- **Live Orders** — what the system currently believes is live on the CLOB
- **Confirmed Orders** — what has been acknowledged or reconstructed via User WebSocket and reconciliation APIs

This separation is intentional. Polymarket processes multiple posted orders in parallel, and intent and confirmed exchange truth are not the same thing at any instant. The system must be resilient to short periods where local intent and exchange state differ, and must reconcile aggressively rather than assume serialized behavior.

The Order Diff and Execution Actor computes the minimum mutation set from Desired → Confirmed. The Liveness Manager and Recovery Coordinator maintain the Live and Confirmed states. On reconnect, Confirmed state is rebuilt via an open-orders query before Desired state is re-evaluated.

### 4.6 Event Flows

**Market Discovery Flow:**
```
Universe Scanner → Capability Enricher → Market Ranker
    → Subscription Manager → Market Stream Gateway
```

**Quote Update Flow:**
```
Market WS event → Book State Store → Quote Engine
    → Order Diff → Risk Gate → Execution Actor
```

**Fill/Reconciliation Flow:**
```
User WS event → Fill & Position Ledger → Inventory Update → Quote Engine
```

**Liveness Recovery Flow:**
```
Heartbeat failure or stream disconnect → Liveness Manager
    → Cancel/Quarantine → Recovery Coordinator → Resume
```

**Reward Optimization Flow:**
```
Rewards config refresh + scoring status + user reward percentages
    → Market Ranker + Quote Engine
```

### 4.7 Order Management Policy

Default order policy:

- **GTD preferred for time-bounded exposure** — use aggressively when the market has a known time boundary (game start, resolution window, event horizon). Prevents stale resting orders from accumulating unintended risk.
- **GTC for open-horizon markets** — appropriate where quote duration being unbounded is acceptable and no near-term event boundary exists.
- **Post-Only for all passive quoting** — prevents paying taker fees and ensures maker rebate eligibility.
- **FOK/FAK reserved for inventory rebalancing** — not for primary quoting.
- **Sports market adapter** — applies game-start cancellation, GTD bounded before `game_start_time`, and 3-second marketable-order delay awareness.

### 4.8 Persistence and Storage

The system uses a two-tier storage model:

**Operational tier (Redis):** Live quote state, Book State Store cache, active order map, fee rate cache, inventory state. Fast reads/writes; acceptable to lose on crash since it is rebuilt from the Ledger and reconciliation APIs on restart.

**Ledger tier (Postgres):** Orders, fills, positions, rewards, rebates, config history. Append-only writes preferred. Provides durable truth for reconciliation, reward attribution, replay, and postmortems.

A fallback to a single encrypted JSON file (as in prior versions) is acceptable for minimal/prototype deployments only. Production deployments must use the two-tier model.

### 4.9 Technology Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python 3.11+ |
| Concurrency | asyncio + uvloop (Linux/macOS only; use default asyncio locally on Windows) |
| Polymarket SDK | `py-clob-client` — pin to a specific release; do not use `latest` |
| Order signing | `eth_account`, `web3.py` pinned to `6.14.0` |
| Operational cache | Redis |
| Ledger storage | Postgres |
| State encryption (fallback) | `cryptography` (Fernet) |
| WebSocket | `websockets` library |
| Blockchain | Polygon PoS (Chain ID 137); USDC.e; Gnosis Safe |
| Data APIs | Gamma API, Data API, CLOB API |
| Metrics | `prometheus_client` + structured JSON logging |
| Infrastructure | Ubuntu 22.04+ VPS; 2+ vCPU, 4GB+ RAM, 40GB+ NVMe SSD |
| Process management | PM2 or Supervisor |
| Alerting | Telegram and Discord webhooks |

> **web3.py:** Pin to `6.14.0`. Later versions have compatibility issues with Polymarket's conditional token approval flow on Polygon.

> **py-clob-client:** Pin to a specific version and update only deliberately. Polymarket has made breaking changes without announcement.

> **uvloop:** 2–4× throughput over default asyncio. Linux/macOS only. Use default asyncio loop for local Windows development.

### 4.10 Wallet Architecture

| Sig Type | Wallet | Use |
|----------|--------|-----|
| Type 2 (production) | Gnosis Safe | Proxy wallet via Builder Relayer. Gasless execution. Pre-set token allowances. Deterministic CREATE2 address from EOA signer. |
| Type 0 (failover) | EOA | Standard EIP-712 signing. Pays gas in POL. Requires one-time token approvals. Used for local development and Relayer outage failover. |

### 4.11 Infrastructure Requirements

| Requirement | Specification |
|------------|---------------|
| Operating system | Ubuntu 22.04 LTS or later |
| Minimum hardware | 2+ vCPU, 4GB+ RAM, 40GB+ NVMe SSD |
| Network latency | < 10ms RTT to Polygon RPC; < 50ms RTT to `clob.polymarket.com`. Recommended: US-East (Virginia) or EU-West (Frankfurt) |
| RPC provider | Dedicated Polygon RPC (Alchemy, QuickNode, or Infura). Public RPCs unsuitable |
| Network egress | Unmetered; WebSocket connections must never be throttled or proxied |
| Firewall | Outbound HTTPS/WSS (443) to Polymarket API domains. Inbound: SSH (22) only |

### 4.12 Authentication Architecture

| Level | Mechanism | Description |
|-------|-----------|-------------|
| Level 1 | EIP-712 Typed Signing | Private key derives API credentials (`apiKey`, `secret`, `passphrase`) via `create_or_derive_api_creds()`. One-time per wallet. |
| Level 2 | HMAC-SHA256 | All API requests signed with derived credentials. Private key not used after credential derivation. |

---

## 5. Trading Strategies

### 5.1 Strategy A: Event-Driven Market Making

#### 5.1.1 Overview

The primary revenue driver. The bot places simultaneous Post-Only limit orders around the order book midpoint, capturing the bid-ask spread as both sides fill. The Quote Engine is market-capability-aware: fee rates, tick sizes, reward configurations, and inventory state are all live inputs. The quote lifecycle is driven entirely by WebSocket events.

#### 5.1.2 The Sub-100ms Execution Pipeline

The full pipeline from WebSocket event receipt to new orders dispatched must complete in under 100ms:

```
WS event received
  → local order book updated          (~1ms)
  → capability cache lookup           (~1ms)
  → quote engine evaluates            (~2ms)
  → fee calculator (cache lookup)     (~1ms; defer one cycle on miss)
  → order diff (desired vs confirmed) (~1ms)
  → risk gate checks                  (~1ms)
  → order executor: batch cancel      (~30ms API round-trip)
  → order executor: batch place       (~30ms API round-trip)
  → confirmation logged               (~1ms)
Total target:                          < 100ms
```

**Cancel-before-place sequencing:** The batch cancel call is dispatched first. The bot does not wait for cancel acknowledgement before dispatching the batch place call — both are fired in immediate succession. The docs document that `POST /orders` (batch) processes orders in parallel, but do not explicitly document the sequencing guarantee between separate cancel and place requests from the same API key. The assumption that the cancel is applied before the placement is evaluated is an **implementation assumption, not a documented guarantee**.

**Retry policy on placement rejection:** If the place call is rejected due to a duplicate order ID (evidence the cancel has not yet propagated), the Order Executor shall apply the following retry sequence rather than a single fixed backoff:

```
Attempt 1: retry after 10ms
Attempt 2: retry after 25ms
Attempt 3: retry after 50ms
After attempt 3 fails:
  → trigger open-orders query (force reconciliation)
  → resync Confirmed state
  → skip this quote cycle for this market
  → log as a sequencing failure
```

**Adaptive confirm-cancel mode:** The Order Executor shall track the placement rejection rate (duplicate-ID rejections as a percentage of all placements) over a rolling 60-second window per market. If this rate exceeds `CANCEL_CONFIRM_THRESHOLD_PCT` (default: 5%), the executor shall automatically switch that market to **confirm-cancel-then-place** mode: await cancel acknowledgement before submitting the replacement. This mode persists until the rejection rate drops below the threshold over a subsequent 60-second window, at which point fire-and-forget resumes. The mode transition shall be logged and alerted. This sequencing behavior must be validated in live testing during the shadow run and early ramp period.

**Fee cache:** Fee rates are cached per market with a 30-second TTL. On a single cache miss, placement is deferred one event cycle. After `FEE_CONSECUTIVE_MISS_THRESHOLD` consecutive misses, that market's quotes are cancelled, it is excluded from new entries, and an alert is sent. Quoting resumes when the cache warms on the next scan cycle.

TTL-based expiry is not sufficient protection against intraday fee changes, which can occur on any market without notice. Two additional invalidation triggers are therefore required: **(a) Fill-event re-fetch:** On every fill notification from the User channel, the fee rate for that market shall be re-fetched immediately and the cache entry overridden. A fill is the highest-signal event that the market is active and fees are consequential. **(b) Deviation-triggered invalidation:** When the cache is refreshed and the new `fee_rate_bps` differs from the cached value by more than `FEE_DEVIATION_THRESHOLD_PCT` (default: 10%), the cache entry shall be invalidated immediately and all active quotes for that market re-evaluated against the new rate within one event cycle per FR-156.

#### 5.1.3 Entry Criteria

1. Market is active and not within 2 hours of resolution
2. Current bid-ask spread exceeds `min_spread = max(BASE_SPREAD, 2 × fee_rate_decimal + COST_FLOOR)` where `fee_rate_decimal = fee_rate_bps / 10000` (pre-entry market spread check)
3. `fee_rate_bps > 100`: do not enter unless observed spread is at least 3× the fee rate
4. Midpoint price is between 0.05 and 0.95
5. Current position is below the per-market limit
6. Our quoted spread after placement must still exceed 3¢ (with `MM_BASE_SPREAD = 0.04` this criterion is typically the binding constraint, requiring an observed spread of ~5¢ before entry)
7. No resolution warning active for this market
8. `accepting_orders = true`
9. `seconds_delay = 0`
10. Market is not flagged for sports-specific cancellation (see Sports Market Adapter, §4.4.2)

#### 5.1.4 Quote Positioning and Order Duration

- Bid = `best_bid + 0.01` (improve by one tick)
- Ask = `best_ask - 0.01` (improve by one tick)
- Order size: configurable (default: 10 shares per side; ramp default: 5 shares)
- **Order type: Post-Only**
- **Duration: GTD preferred when the market has a known time boundary** (e.g., within 4 hours of resolution or game start). GTD expiration is computed as `target_cutoff_unix - buffer + 60`, where the `+ 60` accounts for the platform's documented 1-minute security threshold (the effective lifetime of a GTD order is the specified `expiration` minus 60 seconds). Set `target_cutoff` to `resolutionTime` or `game_start_time` as appropriate, with the configured buffer subtracted. **GTC** for markets with no near-term event horizon. Both must be Post-Only.
- Cancel and replace triggered on every qualifying WebSocket order book delta

#### 5.1.5 Inventory Skew Management

Share-count skew ignores mark price and produces misleading signals near extremes. A YES position of 100 shares at p=0.95 represents ~$95 of expected value; the same count at p=0.50 represents ~$50. The system uses **value-weighted skew** to reflect actual economic exposure:

```
YES_value = YES_shares × YES_price
NO_value  = NO_shares × (1 - YES_price)

skew = (YES_value - NO_value) / (YES_value + NO_value)
```

`YES_price` is the live midpoint read from the Book State Store. `skew` ranges from -1.0 to +1.0.

- **`|skew|` exceeds skew threshold** (default 0.65): apply quote offset of `skew × INVENTORY_SKEW_MULTIPLIER` ticks. Overweight YES → lower YES bid and raise NO ask.
- **Ratio > `INVENTORY_HALT_THRESHOLD`** (default 80:20): suspend quote placement for that market, send alert.
- **Ratio falls below `INVENTORY_RESUME_THRESHOLD`** (default 70:30): resume automatically.

#### 5.1.6 Reward and Rebate Optimization

On Liquidity Rewards-eligible markets, the Quote Engine uses the live `rewardsMaxSpread`, `rewardsMinSize`, and `adjustedMidpoint` fields to position quotes optimally. Order scoring status is monitored to confirm quotes are actually earning rewards. On Maker Rebate-eligible markets, all orders are Post-Only. Taker execution on rebate markets is logged as a rebate-efficiency failure.

---

### 5.2 Strategy B: Penny Option Scooping

#### 5.2.1 Overview

The bot purchases shares priced at $0.001–$0.03 across hundreds of markets. Most expire worthless, but rare hits deliver 100x–500x returns. Strategy B uses taker (market) orders and operates on a 1-minute polling cycle — it is not subject to the sub-100ms constraint. It has its own bankroll limit, kill switch, and P&L attribution.

#### 5.2.2 Market Selection Criteria

- Share price between `PENNY_MIN_PRICE` ($0.001) and `PENNY_MAX_PRICE` ($0.03)
- Market has not resolved; resolution at least 24 hours away
- Priority categories: crypto price predictions (extreme bearish/bullish), esports minor leagues, weather events, niche political outcomes
- Maximum: `PENNY_BUDGET` ($5) per trade; `PENNY_MAX_TOTAL` ($200) total penny positions

#### 5.2.3 Historical Performance Reference (planktonXD, 2025)

| Market | Entry | Cost | Return | ROI |
|--------|-------|------|--------|-----|
| SOL < $130 (Jan 12–18) | $0.007 | $16 | $1,574 | 9,285% |
| VALORANT Fuego win | $0.001 | $3.66 | $874 | 23,750% |

---

### 5.3 Strategy C: Resolution-Window Sniping

#### 5.3.1 Overview

Exploits a recurring mispricing: as markets enter the 4-to-2-hour pre-resolution window with high certainty (probability > 0.90 or < 0.10), prices often lag due to thin order books and slow participants. The bot places Post-Only GTD bids at a dynamic offset below the prevailing ask (between 1¢ and 2¢, scaled to the live spread — see §5.3.3), with expiry set to `resolutionTime - GTD_RESOLUTION_BUFFER_MS // 1000 + 60`. GTD is required here because the pre-resolution window is time-bounded — Design Principle P3 specifies GTD for exactly this scenario; GTC would accumulate risk if the market extends. Entry is restricted to markets where `fee_rate_bps < SNIPE_MAX_FEE_BPS` (near-zero fee zone). Strategy C has its own bankroll limit, kill switch, and P&L attribution.

#### 5.3.2 Entry Criteria and Operating Window

Effective entry window: **4 hours to 2 hours before resolution**. The 4-hour minimum is the outer bound; the 2-hour resolution entry ban (FR-214) is the inner bound.

Entry conditions:
- `YES_prob > SNIPE_PROB_THRESHOLD` OR `YES_prob < (1 - SNIPE_PROB_THRESHOLD)` (default: 0.90)
- At least 4 hours until resolution
- Bid-ask spread in target direction at least 2¢
- `fee_rate_bps < SNIPE_MAX_FEE_BPS` (default: 5) — hard gate
- Position in this market across any single outcome < `SNIPE_MAX_POSITION` ($50)
- Market is not negRisk-flagged

#### 5.3.3 Quote Positioning

The quote offset adapts to the live spread rather than using a hardcoded 2¢. As resolution approaches, spreads typically tighten and informed participants dominate; a fixed offset would become increasingly uncompetitive or over-aggressive. The dynamic offset is:

```
offset = max(0.01, min(0.02, spread × 0.5))
```

where `spread` is `ask - bid` in the target direction at the time of order placement. This produces an offset between 1¢ and 2¢, scaling with actual market liquidity.

- YES > 0.90: maker bid at `YES_ask - offset`
- NO > 0.90 (YES < 0.10): maker bid on NO side at `NO_ask - offset`
- Order size:

```
prob_certainty = YES_prob  (if YES > threshold)
              = 1 - YES_prob  (if YES < 1 - threshold)

size = SNIPE_MIN_SIZE + floor(
    (prob_certainty - SNIPE_PROB_THRESHOLD) /
    (1.0 - SNIPE_PROB_THRESHOLD)
    × (SNIPE_MAX_SIZE - SNIPE_MIN_SIZE)
)
```

Example: threshold = 0.90, YES_prob = 0.95 → certainty = 0.95 → size = 5 + floor(0.5 × 15) = **12 shares**

- Order type: **GTD, Post-Only**. Expiration: `resolutionTime - (GTD_RESOLUTION_BUFFER_MS // 1000) + 60`. This is the correct order type for the pre-resolution window — the window is time-bounded by definition, and GTD prevents stale orders from surviving a market extension.

#### 5.3.4 Resolution Watchlist Interaction

Strategy C orders are GTD with expiry set to `resolutionTime - GTD_RESOLUTION_BUFFER_MS // 1000 + 60`. They will expire naturally before the 2-hour warning threshold in most cases. As a belt-and-suspenders safety measure, any Strategy C GTD orders that remain active when the market enters the **2-hour** warning window are cancelled by the standard cancellation sweep — the GTD expiry means this should be a no-op in normal operation. When the market crosses the **30-minute** threshold, all open orders are cancelled without exception.

---

## 6. Functional Requirements

### 6.1 Connectivity and Data Ingestion (FR-100)

| ID | Requirement |
|----|-------------|
| FR-101 | System shall fetch all active, non-closed events from the Gamma API with pagination (50 per page) |
| FR-102 | System shall extract token IDs, outcome prices, tick sizes, `minimum_order_size`, `seconds_delay`, `negRisk` flags, `game_start_time`, volume, tags, and `accepting_orders` status for each market |
| FR-103 | System shall rescan the full market catalog at `SCAN_INTERVAL_MS` (default: 5 minutes) |
| FR-103a | System shall detect and respond to the following market metadata mutations between rescan cycles, treating them as first-class operational events rather than routine updates: **(1) `resolutionTime` change:** When a market's `resolutionTime` shifts (extension or early resolution), the system shall immediately recompute all GTD expiry timestamps for active quotes in that market, cancel any orders whose expiry is now inconsistent, reprice and replace with corrected expiry, and update the resolution watchlist. Strategy C positions shall be re-evaluated against the new operating window. **(2) `accepting_orders` flip to `false`:** Immediately cancel all active quotes for that market (override of standard cancel path); exclude from new entries until the flag returns to `true` on a subsequent rescan. **(3) `fee_rate_bps` change detected on rescan:** Trigger immediate cache invalidation and quote re-evaluation per FR-156, regardless of TTL. **(4) `seconds_delay` becoming non-zero:** Immediately cancel all active quotes and exclude from new entries per FR-118. Mutations shall be detected by comparing the newly scanned value against the value stored in the Capability Enricher's per-market model from the previous cycle |
| FR-104 | System shall categorize markets by tag (sports, crypto, politics, weather, esports) and route to appropriate market adapters |
| FR-105 | System shall connect to the Polymarket market WebSocket channel and subscribe to target token IDs |
| FR-106 | System shall maintain a local order book (Book State Store) per subscribed token, updated incrementally on each WS message. WebSocket desync is a normal operational event, not an edge case. The Book State Store shall be resynced via a direct `get_order_book()` REST call in the following circumstances: (1) immediately after any sequence of `BOOK_RESYNC_DELTA_THRESHOLD` consecutive missed or out-of-sequence WS deltas (default: 5) — this is the primary trigger; (2) as a periodic safety net at `BOOK_RESYNC_INTERVAL_S` seconds (default: 60, not 10) per market — the interval is intentionally conservative because Polymarket guidance for market makers emphasises WebSocket-driven quoting over REST polling; frequent periodic polling adds unnecessary baseline REST traffic across all active subscriptions. **The periodic interval should be treated as a last-resort catch, not a primary synchronisation mechanism.** If live evidence shows WebSocket desync is frequent enough to require a shorter interval, reduce `BOOK_RESYNC_INTERVAL_S` operationally rather than baking a short default into the initial deployment. On resync, the local book is atomically replaced with the REST response in full. The resync call is excluded from the sub-100ms execution pipeline. **Quoting policy during resync:** When resync starts, set `resyncing = true` for that market. During the resync window: (a) no new quote placements are submitted; (b) cancels are permitted and may be submitted if the escalation condition below is met. **Default behavior (stable market):** existing resting quotes remain live during the resync window — the exposure is bounded and identical to normal inter-event exposure. **Escalation condition (cancel immediately):** if any of the following are true at resync start, all active quotes for that market are cancelled immediately before the REST call completes: (i) mid-price moved more than `BOOK_RESYNC_CANCEL_MID_PCT` (default: 0.5%) since the last successfully processed WS update; (ii) observed spread at last WS update exceeded `BOOK_RESYNC_CANCEL_SPREAD_TICKS` ticks (default: 10); (iii) the WS gap duration that triggered this resync exceeded `BOOK_RESYNC_CANCEL_GAP_MS` (default: 2000ms). **After resync completes:** atomically replace the Book State Store, clear `resyncing`, then immediately recompute quotes without waiting for the next WS event — treat resync completion as an implicit trigger for the quote cycle. **In-flight order acknowledgements during resync:** Orders submitted before `resyncing = true` was set are treated as valid intents; their acknowledgements update Confirmed state normally during the resync window. However, those confirmed orders are not re-evaluated against Desired state until the post-resync quote recompute completes — this prevents stale Desired state from immediately cancelling or replacing orders that may now be correctly positioned relative to the fresh book |
| FR-107 | System shall connect to the authenticated user WebSocket channel for fill and cancellation notifications |
| FR-108 | System shall implement automatic WebSocket reconnection with exponential backoff (1s initial, 30s max) |
| FR-109 | System shall support dynamic subscription: adding and removing token IDs without disconnecting |
| FR-110 | System shall flag markets eligible for Liquidity Rewards and Maker Rebates and route them to the Quote Engine with live reward parameters |
| FR-111 | System shall connect to a dedicated Polygon RPC endpoint. Abort startup if RTT > `RPC_MAX_LATENCY_MS` |
| FR-112 | At startup, system shall derive CLOB API credentials via `create_or_derive_api_creds()` (Level 1) and use HMAC-SHA256 for subsequent requests (Level 2). Private key not used after credential derivation |
| FR-113 | System shall verify USDC.e balance at startup and abort if below `MIN_USDC_BALANCE`. Warn if balance drops below 120% of `MAX_TOTAL_EXPOSURE` during operation |
| FR-114 | System shall implement the two mandatory liveness loops as specified in FR-501: (1) the order-safety heartbeat (POST to CLOB API every 5 seconds) and (2) the Market/User channel WebSocket application heartbeat (client sends literal `PING` every 10 seconds, expects `PONG`). If the Sports WebSocket channel is opened, a third loop is required: server sends `ping` every 5 seconds, client must reply `pong` within 10 seconds. All active loops are owned by the Liveness Manager and must run as independent asyncio tasks |
| FR-115 | On reconnection, system shall query open orders to reconcile Confirmed order state. Orders believed Live but absent from server response shall be marked cancelled and removed from Confirmed state |
| FR-116 | System shall check `accepting_orders` per market before placing any order. Market Scanner is responsible for keeping this flag current. This is the authoritative enforcement point |
| FR-117 | System shall extract and propagate the `negRisk` flag. For negRisk-flagged markets, `negRisk = true` must be set in all CLOB order payloads. Strategy A may enter negRisk markets with correct flag propagation. Strategy C shall exclude negRisk markets |
| FR-118 | System shall not submit orders to any market where `seconds_delay > 0`. Skip silently until delay clears on next rescan |
| FR-119 | System shall apply the Sports Market Adapter to all markets with a non-null `game_start_time`. Adapter behavior: (1) all open quotes are cancelled at `game_start_time`; (2) GTD expiry for new quotes must be set before `game_start_time`; (3) marketable orders for sports markets carry an additional 3-second processing delay that must be accounted for in execution latency budgets |

### 6.2 Capability Enrichment (FR-150)

| ID | Requirement |
|----|-------------|
| FR-151 | System shall obtain `feeRateBps` per market via one of two paths: (1) **SDK auto-handling** — when using `py-clob-client` or `@polymarket/clob-client` order creation methods, the SDK fetches the fee rate and embeds `feeRateBps` in the signed payload automatically; (2) **Custom signing** — call the dedicated `GET /fee-rate?token_id={id}` CLOB endpoint directly. Path (1) is preferred. Results shall be cached with TTL of `FEE_CACHE_TTL_S`. On single cache miss, defer one event cycle. After `FEE_CONSECUTIVE_MISS_THRESHOLD` consecutive misses, cancel market quotes, exclude from new entries, send alert. Resume when cache warms. Additionally: (a) on every fill event for a market, re-fetch that market's fee rate immediately and override the cache entry; (b) on any cache refresh where the new value deviates from the cached value by more than `FEE_DEVIATION_THRESHOLD_PCT`, invalidate the cache entry immediately and trigger quote re-evaluation per FR-156 |
| FR-152 | System shall embed the resolved `feeRateBps` value in every EIP-712 signed order payload. Omitting this field on a fee-eligible market is a fatal signing error |
| FR-153 | System shall compute minimum profitable spread as `max(BASE_SPREAD, 2 × fee_rate_decimal + COST_FLOOR)` where `fee_rate_decimal = fee_rate_bps / 10000`. Example: `fee_rate_bps = 78` → `fee_rate_decimal = 0.0078` → `min_spread = max(0.04, 0.0256) = 0.04`. This formula is evaluated first; FR-154 is an additional filter |
| FR-154 | Strategy A shall not enter a market where `fee_rate_bps > 100` unless observed spread is at least 3× the fee rate. Both FR-153 and FR-154 must pass |
| FR-155 | Strategy C shall only enter markets where `fee_rate_bps < SNIPE_MAX_FEE_BPS`. Hard gate |
| FR-156 | On fee cache refresh, or on immediate invalidation triggered by a fill event or fee rate deviation (FR-151), if the recalculated minimum spread for an active market is no longer met, cancel all quotes for that market within one event cycle |
| FR-157 | System shall obtain reward parameters for eligible markets from the dedicated CLOB rewards endpoints as the primary source of truth, with Gamma API reward surfaces as a secondary supplement. The canonical reward endpoint hierarchy is: **(1) `GET /rewards/markets/current`** — current reward configuration per market including min size, max spread, daily rate; **(2) `GET /rewards/markets/multi`** — batch reward config for multiple markets; **(3) `GET /rewards/user/percentages`** — live user reward percentage per market; **(4) `GET /order-scoring`** — whether a specific live order is scoring for rewards; requires `order_id` as a query parameter; evaluate active orders by querying this endpoint per order. These dedicated endpoints take precedence over any reward fields on Gamma market objects. Gamma's `markets?rewards=true` endpoint may be used as a supplementary source for bulk candidate discovery, but scoring status and live user percentages must always come from (3) and (4). All reward values must be refreshed at each rescan cycle and used as live inputs to the Quote Engine for reward-eligible markets |
| FR-158 | System shall monitor order scoring status for reward-eligible markets by querying `GET /order-scoring?order_id={id}` for each active order. This endpoint checks one order at a time — the system must iterate over active orders and query per order. Unscored orders shall be flagged in the daily summary as reward-capture failures |

### 6.3 Order Execution and Management (FR-200)

| ID | Requirement |
|----|-------------|
| FR-201 | System shall place limit orders via `ClobClient.create_and_post_order()` with correct `tokenID`, `price`, `size`, `side`, `tickSize`, `negRisk`, `feeRateBps`, and `expiration` (for GTD). GTD `expiration` must be computed as `target_cutoff_unix - buffer_seconds + 60` to account for the platform's 1-minute security threshold (the effective GTD lifetime is the specified expiration minus 60 seconds). If an order expires meaningfully earlier than the computed expiry (observable by comparing fill/cancel events against expected lifetime), the system shall log the discrepancy with the expected vs. actual lifetime and adjust the offset dynamically — increasing the `+ 60` correction — until behavior stabilises. This guards against any unannounced change to the platform's security threshold |
| FR-202 | System shall round prices to 2dp for standard markets and 3dp for prices < 0.04 or > 0.96. System shall validate order size against each market's `minimum_order_size`; if configured size is below market minimum, raise to minimum and log the adjustment |
| FR-203 | All orders must be EIP-712 signed messages |
| FR-204 | Production mode shall use Gnosis Safe (signature type 2) via Builder Relayer. EOA (type 0) is used only for Relayer failover (FR-216) and local development |
| FR-205 | System shall deploy the Gnosis Safe proxy wallet on first run via `RelayClient.deploy()` and cache the proxy address |
| FR-206 | System shall use the batch order API for all cancel and place operations (up to 15 orders per call; verify current limit against CLOB API docs before deployment) |
| FR-207 | The cancel/replace pipeline should target < 100ms P95 end-to-end as a desk target under normal operating conditions. "On wire" is defined as the timestamp when the HTTP request is dispatched, not when the response is received. Alert if P95 exceeds 80ms. Sustained P95 exceedance above `LATENCY_ALERT_P95_MS` is treated as an infrastructure incident or platform-side throttling event — not necessarily a code defect. Cloudflare throttling, exchange pauses, and matching-engine restarts are documented platform-side conditions that can push latency above any internal target |
| FR-208 | System shall handle HTTP 429 responses with exponential backoff |
| FR-209 | System shall enforce the API rate limit via internal throttling |
| FR-210 | All Strategy A and Strategy C orders shall be Post-Only. REST polling is prohibited in the Strategy A and C execution path. Strategy B may use polling cycles of up to 1 minute |
| FR-210a | Before placing any order on a given side for a market, the Order Diff Actor shall check whether an own resting order exists on the opposing side within 1 tick of the proposed placement price. If such an order exists, it shall be cancelled before the new order is placed. This prevents self-crossing, unnecessary internal fills, fee inefficiency, and distorted P&L |
| FR-211 | System shall support a global kill switch cancelling all open orders within 5 seconds via `cancel_all()`. In-flight CTF redemption transactions (FR-215) shall be allowed to complete. Queued-but-unsubmitted redemptions shall be logged as `redemption_interrupted = true` and alerted |
| FR-212 | System shall cancel stale quotes not refreshed within `STALE_QUOTE_TIMEOUT_S` as a safety net |
| FR-213 | When a market enters the 30-minute pre-resolution window, all open quotes shall be cancelled immediately. Strategy C orders are GTD and will typically have expired naturally before this point; any remaining Strategy C orders are included in the cancellation sweep without exception |
| FR-214 | Resolution watchlist: markets within `RESOLUTION_WARN_MS` excluded from new entries; markets within `RESOLUTION_PULL_MS` trigger forced cancellation |
| FR-215 | Auto-redemption: (1) Poll Gamma API for `resolved = true` at `REDEMPTION_POLL_INTERVAL_S` intervals. (2) Call the CTF contract: `redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)` where `collateralToken` is the USDC.e contract address on Polygon, `parentCollectionId` is `bytes32(0)`, `conditionId` is the market's condition ID, and `indexSets` is a one-element bitmask array (e.g., `[1]` for outcome index 0, `[2]` for outcome index 1). (3) Submit via Builder Relayer for gasless execution. (4) Submit within 1 hour of resolution confirmation. (5) On revert or timeout, retry up to 3 times with backoff (30s, 120s, 300s). (6) After 3 failures, log and alert for manual redemption. (7) Write redeemed market IDs to ledger immediately after on-chain confirmation to prevent double-redemption. (8) Alert on each successful redemption with market name, outcome, and USDC received |
| FR-216 | Relayer failover: if Relayer is unreachable for `EOA_FALLBACK_TIMEOUT_S`, switch to direct EOA execution. Alert on failover and recovery. Revert to Relayer on recovery. Suspend order submissions if POL balance is insufficient for EOA gas |

### 6.4 Risk Management (FR-300)

| ID | Requirement |
|----|-------------|
| FR-301 | Enforce maximum total portfolio exposure (default: $2,000) |
| FR-302 | Enforce per-market position limit (default: $100) |
| FR-303 | Enforce daily loss limit; halt all trading if daily P&L falls below threshold (default: –$500) |
| FR-304 | Enforce maximum drawdown from peak equity; halt if exceeded (default: $500) |
| FR-305 | Perform pre-trade risk validation (Risk Gate) before every order submission |
| FR-306 | Track YES/NO inventory per market using value-weighted skew per §5.1.5 (`skew = (YES_value - NO_value) / (YES_value + NO_value)`). Apply skew adjustment when skew exceeds `INVENTORY_SKEW_THRESHOLD`. Halt and alert when skew exceeds `INVENTORY_HALT_THRESHOLD`. Resume below `INVENTORY_RESUME_THRESHOLD` |
| FR-307 | Reset daily counters at 00:00 UTC; resume trading if halted due to daily limits |
| FR-308 | Log all risk events with timestamps |
| FR-309 | Risk Gate shall verify as defense-in-depth that the `accepting_orders` gate (authoritative in FR-116) has been applied before order submission. Breach logged as a data-layer validation failure |
| FR-310 | Monitor pipeline latency continuously. If P95 exceeds `LATENCY_ALERT_P95_MS` for 60 consecutive seconds, alert and reduce active subscription count by 50% until latency recovers |

### 6.5 Liquidity Rewards Optimization (FR-400)

| ID | Requirement |
|----|-------------|
| FR-401 | Identify reward-eligible market candidates using `gamma-api.polymarket.com/markets?rewards=true` during each rescan cycle. This Gamma endpoint is used for **candidate discovery only** — to build the set of token IDs that may be reward-eligible. It must not be used as the canonical source for reward configuration values. Once candidate markets are identified, all reward configuration inputs (`rewardsMinSize`, `rewardsMaxSpread`, `rewardsDailyRate`, `adjustedMidpoint`, reward percentages, and order scoring status) must be fetched from the dedicated CLOB rewards endpoints per FR-157 (`GET /rewards/markets/current`, `GET /rewards/markets/multi`, `GET /rewards/user/percentages`, `GET /order-scoring` per order). This two-step approach reflects that Gamma provides discovery coverage while the CLOB rewards endpoints provide authoritative configuration |
| FR-402 | Place quotes within `rewardsMaxSpread` of the `adjustedMidpoint` for reward-eligible markets |
| FR-403 | Order size on reward-eligible markets shall meet or exceed `rewardsMinSize` |
| FR-404 | When `adjustedMidpoint` is below $0.10, maintain quotes on both YES and NO sides |
| FR-405 | Target inner ticks closest to `adjustedMidpoint` (quadratic reward formula rewards proximity) |

### 6.6 Maker Rebates Integration (FR-450)

| ID | Requirement |
|----|-------------|
| FR-451 | Identify Maker Rebate-eligible markets via the `feesEnabled` flag on the market object, which is the documented authoritative switch for fee and rebate eligibility. Do not rely on a static category list and do not key off `taker_base_fee` — that field is not documented as the canonical eligibility signal in the current official docs |
| FR-452 | All orders on rebate-eligible markets shall be Post-Only. Taker execution is logged as a rebate-efficiency failure |
| FR-453 | Target 100% maker-to-taker ratio for Strategies A and C. Strategy B taker orders excluded. The system shall record the actual maker/taker classification of each fill as reported in the User channel fill event — not infer it from order type alone. Post-Only orders are designed to execute as maker, but fill events carry the authoritative classification. Any fill classified as taker on a Post-Only order shall be logged as a maker-classification anomaly and included in the daily summary. Report daily maker ratio (A+C, by actual fill classification) in the operational summary |

### 6.7 Session Integrity and State Persistence (FR-500)

| ID | Requirement |
|----|-------------|
| FR-501 | The system must maintain three distinct liveness loops, none of which may be conflated: **(1) Order-safety heartbeat:** POST a heartbeat to the CLOB API at `HEARTBEAT_INTERVAL_MS` (default: 5 seconds). The platform cancels all open orders if a valid heartbeat is not received within 10 seconds; the 5-second buffer means the effective safe interval is ≤ 5 seconds. This is the critical production-safety loop. If 2 consecutive heartbeat cycles receive no acknowledgement, treat the session as dead and initiate full reconnection. **(2) Market and User channel WebSocket heartbeat:** The client must send the literal string `PING` as an application-level message every 10 seconds; the server responds with `PONG`. This is not a WebSocket-protocol ping frame — it is a string message sent on the channel. **(3) Sports channel WebSocket heartbeat (conditional):** Only required if the Sports WebSocket channel is opened. If used, the server sends `ping` every 5 seconds and the client must reply `pong` within 10 seconds. The Sports WebSocket is a separate endpoint for live sports results and is not required by the v3 sports adapter, which drives its behavior from `game_start_time` and Gamma market metadata. A failure in any one active loop does not indicate a failure in the others |
| FR-502 | On any reconnection, query open orders to rebuild Confirmed order state |
| FR-503 | Persist critical operational state to Redis (live quote state, order map, fee cache, inventory state). Persist durable state to Postgres (orders, fills, positions, rewards, rebates). For minimal deployments: persist to an encrypted local JSON file every 60 seconds with atomic writes (write to `.tmp`, then rename). Production deployments must use Redis + Postgres. Schema changes must be versioned and applied via forward-only migrations; backward-incompatible changes require a controlled deployment with reconciliation before the new process version begins quoting |
| FR-504 | On startup, rebuild state from Postgres/Order Ledger (orders) and Data API (positions). Do not depend on any single local file as the sole source of truth |
| FR-504a | The system shall define and implement explicit degraded-mode behavior for storage failures: **(1) Redis outage:** Operational state (live quotes, order map, fee cache, inventory) is held in process memory only. Quoting continues normally. The degraded mode is logged and alerted immediately. On Redis recovery, state is flushed from memory to Redis. If Redis is unavailable for more than `REDIS_OUTAGE_HALT_S` (default: 300 seconds), the system enters safe mode (see below). **(2) Postgres outage:** Fill and position writes are buffered in an in-process append-only queue (capped at `POSTGRES_BUFFER_MAX_ROWS`, default: 10,000 rows). Quoting continues. On recovery, the buffer is flushed to Postgres in order. If the buffer reaches capacity, new fills are logged to a local fallback file rather than dropped, and an alert is sent. Order placement is not blocked by Postgres unavailability. **(3) Both Redis and Postgres unavailable simultaneously:** The system enters **safe mode** — cancel all open orders, halt new quote placement, and alert. Safe mode persists until at least one storage tier recovers. On recovery, perform full reconciliation via the open-orders query and Data API before resuming. **(4) Safe mode exit:** Quoting resumes only after: open-orders query completes successfully, Confirmed state is rebuilt, and at least one successful durable write to Postgres is confirmed. Alert when safe mode exits and quoting resumes. |
| FR-505 | Maintain resolution watchlist from Gamma API. Markets within 2 hours: no new entries. Markets within 30 minutes: force-cancel all quotes |
| FR-506 | Write redeemed market IDs to ledger immediately after on-chain confirmation to prevent double-redemption |

### 6.8 Observability (FR-600)

| ID | Requirement |
|----|-------------|
| FR-601 | Log all order placements, cancellations, fills, and risk events as structured JSON: `timestamp`, `event_type`, `market_id`, `token_id`, `side`, `price`, `size`, `order_id`, `strategy`, `latency_ms`, `pnl_impact`, `fee_rate_bps`, `order_duration_type` (GTC/GTD) |
| FR-601a | For every Strategy A fill, the Fill and Position Ledger shall record the mid-price at fill time and schedule a 30-second deferred lookup of the mid-price at fill time + 30 seconds. The 30-second markout (`mid_t30 - mid_t0` × side-sign, where positive = adverse) shall be persisted with the fill record. This data feeds the Market Ranker's `adverse_selection_cost` estimate and the markout gate in §11.4 Step 3 |
| FR-602 | Emit status report every 30 seconds: total exposure, daily P&L, drawdown, trade count, active subscriptions, inventory warnings, P95 latency |
| FR-603 | Send Telegram/Discord alerts for: kill switch activation, daily loss limit hit, WebSocket disconnect > 60s, inventory halt, zero trades for 30+ minutes, market resolution with held positions, P95 latency exceeding `LATENCY_ALERT_P95_MS` for 60s, Relayer failover, fee cache sustained outage |
| FR-604 | Emit daily summary at 00:00 UTC: total trades, net P&L, top markets by P&L, exposure by strategy, estimated Liquidity Rewards, estimated Maker Rebates, maker-to-taker ratio (A+C), average latency, order scoring success rate |
| FR-605 | Export Prometheus metrics on `PROMETHEUS_PORT`: `bot_pnl_daily` (Gauge, resets at midnight), `bot_trades_total` (Counter), `bot_latency_p95_ms` (Histogram — buckets: 10, 25, 50, 75, 100, 150, 200ms), `bot_maker_ratio` (Gauge, rolling 1-hour, A+C only), `bot_exposure_total` (Gauge, USD), `bot_drawdown` (Gauge, USD from peak) |
| FR-606 | Attach Builder attribution headers to all orders for Builder Leaderboard credit |

---

## 7. Non-Functional Requirements

### 7.1 Performance

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-101 | Cancel/replace pipeline latency (WS event to orders on wire) | < 100ms P95 — measured, not guaranteed. The budget assumes ~30ms per API round-trip under normal network conditions. Infrastructure degradation, Relayer latency, or rate-limit backoff will push this figure higher. Monitor P95 continuously via Prometheus and treat sustained exceedance as an infrastructure incident, not a code defect |
| NFR-102 | WebSocket message processing latency | < 10ms per message |
| NFR-103 | Full market catalog scan duration | < 3 minutes for 5,000+ markets |
| NFR-104 | Memory usage under 50 active subscriptions | < 512MB RSS |
| NFR-105 | System uptime | 99.5%+ |
| NFR-106 | Fee rate cache hit ratio | > 95% |

### 7.2 Reliability

- WebSocket auto-reconnection with exponential backoff (1s → 30s max)
- HTTP 429 retry with exponential backoff
- Graceful shutdown on SIGINT/SIGTERM: `cancel_all()` before exit
- PM2 or Supervisor for automatic restart on crash
- Builder Relayer failover to EOA if unreachable > `EOA_FALLBACK_TIMEOUT_S` (FR-216)

### 7.3 Security

1. Private keys in environment variables only — never in code or config files
2. API credentials derived at startup and held in memory only
3. Dedicated wallet with limited USDC.e balance (trading float only)
4. VPS: SSH key authentication; root login disabled; fail2ban configured
5. Redis and Postgres access restricted to localhost or private VPC
6. No logging of private key material, API secrets, or wallet mnemonics at any log level
7. `.env` excluded from version control

### 7.4 Observability SLAs

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-201 | Structured log retention | 90 days minimum |
| NFR-202 | Alert delivery latency | < 60 seconds from trigger |
| NFR-203 | Audit log completeness | 100% of orders, fills, cancellations |
| NFR-204 | Prometheus scrape interval | 15 seconds |

---

## 8. API Integration Specification

### 8.1 Polymarket CLOB API

| Endpoint / Method | Type | Usage |
|-------------------|------|-------|
| `create_and_post_order()` | POST | Place limit order with `tokenID`, `price`, `size`, `side`, `tickSize`, `negRisk`, `feeRateBps`, `expiration` (GTD). Supports GTC, GTD, Post-Only. GTD `expiration` = `target_cutoff_unix - buffer + 60` (platform applies a 1-minute security threshold) |
| `cancel_order()` | DELETE | Cancel a specific order by ID |
| `cancel_orders()` / `cancel_all()` | DELETE | Batch cancel. Used for kill switch and resolution quote-pull |
| Open-orders query | GET | List all active orders for the authenticated user. Called via the authenticated orders endpoint on every reconnection to rebuild Confirmed state |
| `get_order_book(token_id)` | GET | Fetch bids/asks plus capability metadata: `tick_size`, `neg_risk`, `min_order_size`, `last_trade_price`. Used for Book State Store initialization and capability cache warm. Does not return `feeRateBps` |
| `GET /fee-rate?token_id={id}` | GET | Dedicated fee rate endpoint. Returns current `feeRateBps` for the specified token. Use for custom signing pipelines. SDK auto-handles this for standard order creation |
| Batch order endpoint | POST | Up to 15 orders or cancellations per call. Verify current limit against CLOB API docs before deployment |

**Fee rate handling:** The official SDKs (`py-clob-client`, `@polymarket/clob-client`) automatically fetch the current fee rate and include `feeRateBps` in the signed order payload when using their standard order creation methods. For custom signing pipelines, use the dedicated `GET /fee-rate?token_id={id}` endpoint. Do not rely on the order book response for fee rate data — the current CLOB order book endpoint documents `tick_size`, `neg_risk`, `min_order_size`, and `last_trade_price`, but not `feeRateBps`.

### 8.2 Builder Relayer API

| Method | Usage |
|--------|-------|
| `RelayClient.deploy()` | Deploy Gnosis Safe proxy wallet. One-time first-run operation |
| `RelayClient.execute(txs)` | Submit gasless transactions |
| `RelayClient.get_deployed()` | Check if Safe is already deployed at startup |

### 8.3 Gamma API (Market Discovery)

| Endpoint | Method | Usage |
|----------|--------|-------|
| `gamma-api.polymarket.com/events` | GET | List active events with pagination, tag, and status filters |
| `gamma-api.polymarket.com/markets` | GET | List markets; filter by `tag_id`, `slug`, `condition_id`, `accepting_orders` |
| `gamma-api.polymarket.com/markets?rewards=true` | GET | Supplementary reward candidate discovery. Key fields: `rewardsMinSize`, `rewardsMaxSpread`, `rewardsDailyRate`, `adjustedMidpoint`. Use for bulk candidate discovery only — see §8.3a for authoritative reward data |
| `gamma-api.polymarket.com/markets/{condition_id}` | GET | Single market detail: `resolved`, `resolutionTime`, `fee_rate_bps`, `seconds_delay`, `minimum_order_size`, `game_start_time`. Used for resolution polling and capability enrichment |

### 8.3a Dedicated Rewards Endpoints (Primary Source of Truth)

These endpoints are authoritative for reward configuration and scoring status.
They take precedence over any reward fields on Gamma market objects (FR-157).

| Endpoint | Method | Usage |
|----------|--------|-------|
| `GET /rewards/markets/current` | GET | Current reward configuration per market: min size, max spread, daily rate. Refresh each rescan cycle |
| `GET /rewards/markets/multi` | GET | Batch reward config for multiple markets in a single call. Use for bulk enrichment |
| `GET /rewards/user/percentages` | GET | Live user reward percentage per market. Use as Quote Engine input for reward-eligible markets |
| `GET /order-scoring` | GET | Whether a specific live order is scoring for rewards. Required query parameter: `order_id`. Evaluate active orders by querying this endpoint per order — it does not return status for the entire live book in one call. Monitor per FR-158 |

### 8.4 WebSocket Feeds

| Channel | URL Path | Data | Role |
|---------|----------|------|------|
| Market Channel | `/ws/market` | Real-time order book updates by `assets_ids` | Primary trigger for all Strategy A and C quote management |
| User Channel | `/ws/user` | Order fills, cancellations, position changes | Fill tracking and Confirmed state updates |

REST polling is prohibited for Strategy A and C quote management.

### 8.5 Rate Limits and Constraints

| Constraint | Value |
|-----------|-------|
| POST /order | 3,500/10s burst; 36,000/10min sustained (60/s average) |
| DELETE /order | 3,000/10s burst; 30,000/10min sustained |
| Batch endpoints | 1,000/10s burst; 15,000/10min sustained |
| Batch order size | Up to 15 per call (verify against current CLOB API docs) |
| Gas cost (Relayer mode) | $0 |
| Taker fee (fee-free markets) | 0% |
| Taker fee (eligible markets) | Market-specific; peaks at `p = 0.50`, approaches zero at extremes. Read from `/fee-rate/{token_id}` at runtime — do not assume a fixed figure |
| Tick size (standard) | $0.01 (2dp) |
| Tick size (extreme prices) | $0.001 for p < 0.04 or p > 0.96 (3dp) |

---

## 9. Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DRY_RUN` | `true` | Paper-trading mode; logs signals without submitting orders |
| `STRATEGY_A_ENABLED` | `true` | Enable or disable Strategy A at runtime |
| `STRATEGY_B_ENABLED` | `true` | Enable or disable Strategy B at runtime |
| `STRATEGY_C_ENABLED` | `true` | Enable or disable Strategy C at runtime |
| `STRATEGY_A_UNIVERSE_TAGS` | `[]` | JSON array of Gamma market tags to restrict Strategy A candidate universe. Examples: `["crypto"]` for crypto-only, `["sports","esports"]` for sports and esports, `[]` for all tags (no filter). Set as a JSON array string in the `.env` file. Used to implement the v1 environment focus from §1 without code changes |
| `USE_RELAYER` | `true` | Use Builder Relayer for gasless execution. `false` = direct EOA |
| `POLYGON_RPC_URL` | (required) | Dedicated Polygon RPC endpoint URL |
| `PRIVATE_KEY` | (required) | Wallet private key. Loaded from env var only |
| `BUILDER_API_KEY` | (required) | Builder Program API key |
| `BUILDER_SECRET` | (required) | Builder Program secret |
| `BUILDER_PASSPHRASE` | (required) | Builder Program passphrase |
| `REDIS_URL` | (required for production) | Redis connection string for operational cache |
| `DATABASE_URL` | (required for production) | Postgres connection string for ledger storage |
| `REDIS_OUTAGE_HALT_S` | `300` | Seconds of continuous Redis unavailability before the system enters safe mode |
| `POSTGRES_BUFFER_MAX_ROWS` | `10000` | Maximum in-process buffered rows during Postgres outage before falling back to local file |
| `STATE_FILE_PATH` | `./state.json.enc` | Fallback encrypted JSON state file (minimal deployments only) |
| `STATE_ENCRYPTION_KEY` | (required for fallback) | Fernet key for state file encryption |
| `SCAN_INTERVAL_MS` | `300000` | Full market rescan frequency (5 minutes) |
| `FEE_CACHE_TTL_S` | `30` | Fee rate cache TTL in seconds |
| `FEE_CONSECUTIVE_MISS_THRESHOLD` | `5` | Consecutive cache misses before market exclusion |
| `FEE_DEVIATION_THRESHOLD_PCT` | `10` | Percentage fee rate change that triggers immediate cache invalidation and quote re-evaluation |
| `BOOK_RESYNC_INTERVAL_S` | `60` | Periodic Book State Store resync interval. Conservative default — desync-triggered resync (via `BOOK_RESYNC_DELTA_THRESHOLD`) is the primary mechanism. Reduce operationally if live evidence shows WebSocket desync is frequent |
| `BOOK_RESYNC_DELTA_THRESHOLD` | `5` | Consecutive missed or out-of-sequence WS deltas that trigger an immediate resync |
| `BOOK_RESYNC_CANCEL_MID_PCT` | `0.5` | Mid-price movement threshold (%) since last WS update that triggers quote cancellation at resync start |
| `BOOK_RESYNC_CANCEL_SPREAD_TICKS` | `10` | Spread width threshold (ticks) at last WS update that triggers quote cancellation at resync start |
| `BOOK_RESYNC_CANCEL_GAP_MS` | `2000` | WS gap duration (ms) that triggers quote cancellation at resync start |
| `CANCEL_CONFIRM_THRESHOLD_PCT` | `5` | Duplicate-ID rejection rate (as % of placements over 60s) above which the executor switches a market to confirm-cancel-then-place mode |
| `REDEMPTION_POLL_INTERVAL_S` | `60` | Resolution confirmation polling interval |
| `EOA_FALLBACK_TIMEOUT_S` | `30` | Seconds before activating EOA failover |
| `MM_BASE_SPREAD` | `0.04` | Minimum base spread before fee adjustment |
| `MM_COST_FLOOR` | `0.01` | Minimum spread above fee rate required to enter |
| `MM_ORDER_SIZE` | `10` | Shares per side. Ramp config (§11.5) uses 5 for first 14 live days |
| `MM_MIN_ORDER_SIZE` | `0` | Minimum order size floor. `0` uses each market's native `minimum_order_size` |
| `MM_MAX_MARKETS` | `20` | Maximum simultaneous market-making targets |
| `GTD_RESOLUTION_BUFFER_MS` | `7200000` | Buffer subtracted from `resolutionTime` when computing GTD expiry. Effective GTD lifetime = `resolutionTime - GTD_RESOLUTION_BUFFER_MS - now + 60s` (the +60s accounts for the platform's 1-minute security threshold) |
| `GTD_GAME_START_BUFFER_MS` | `300000` | Buffer subtracted from `game_start_time` when computing GTD expiry for sports markets. Effective GTD lifetime = `game_start_time - GTD_GAME_START_BUFFER_MS - now + 60s` |
| `PENNY_MIN_PRICE` | `0.001` | Minimum share price for penny purchases |
| `PENNY_MAX_PRICE` | `0.03` | Maximum share price for penny purchases |
| `PENNY_BUDGET` | `5` | Max USD per penny trade |
| `PENNY_MAX_TOTAL` | `200` | Maximum total USD in penny positions |
| `SNIPE_PROB_THRESHOLD` | `0.90` | Probability threshold for Strategy C. Entry: `YES_prob > threshold OR YES_prob < (1 - threshold)` |
| `SNIPE_MAX_FEE_BPS` | `5` | Maximum fee rate (bps) for Strategy C entry |
| `SNIPE_MAX_POSITION` | `50` | Maximum USD per Strategy C market (single outcome) |
| `SNIPE_MIN_SIZE` | `5` | Minimum order size (shares) for Strategy C |
| `SNIPE_MAX_SIZE` | `20` | Maximum order size (shares) for Strategy C |
| `MAX_TOTAL_EXPOSURE` | `2000` | Total portfolio exposure cap (USD) |
| `MAX_PER_MARKET` | `100` | Maximum exposure per market (USD) |
| `MAX_DAILY_LOSS` | `500` | Daily loss limit before halt (USD) |
| `MAX_DRAWDOWN` | `500` | Maximum drawdown before halt (USD) |
| `INVENTORY_SKEW_THRESHOLD` | `0.65` | YES/NO ratio at which quote skewing activates |
| `INVENTORY_HALT_THRESHOLD` | `0.80` | YES/NO ratio at which quoting suspends |
| `INVENTORY_RESUME_THRESHOLD` | `0.70` | YES/NO ratio below which quoting resumes |
| `INVENTORY_SKEW_MULTIPLIER` | `3` | Tick-offset multiplier when skew is active |
| `RESOLUTION_WARN_MS` | `7200000` | Time before resolution at which new entries stop (2 hours) |
| `RESOLUTION_PULL_MS` | `1800000` | Time before resolution at which all quotes are force-cancelled (30 minutes) |
| `STALE_QUOTE_TIMEOUT_S` | `60` | Safety-net stale quote cancellation interval |
| `LATENCY_ALERT_P95_MS` | `150` | P95 latency threshold for alert trigger |
| `REQUEST_TIMEOUT_S` | `10` | Maximum seconds to wait for a CLOB API response before treating it as a soft rate-limit signal. Delayed responses beyond this threshold trigger 250ms backoff before retry |
| `HEARTBEAT_INTERVAL_MS` | `5000` | CLOB session heartbeat interval. The docs specify that open orders are cancelled if a valid heartbeat is not received within 10 seconds, with a 5-second buffer. The documented example sends heartbeats every 5 seconds. A 30-second interval is not safe under current documented behavior |
| `MIN_USDC_BALANCE` | `100` | Minimum USDC.e balance at startup. At $500 initial capital this equals the 20% reserve — reduce to $50 if deploying at that level |
| `RPC_MAX_LATENCY_MS` | `100` | Maximum acceptable RTT to Polygon RPC at startup |
| `PROMETHEUS_PORT` | `9090` | Port for Prometheus metrics |
| `TELEGRAM_WEBHOOK_URL` | (optional) | Telegram bot webhook |
| `DISCORD_WEBHOOK_URL` | (optional) | Discord webhook |

---

## 10. Development Milestones

| Phase | Milestone | Deliverables | Duration |
|-------|-----------|-------------|----------|
| 1 | Core Infrastructure | Auth setup, `py-clob-client` integration, Relayer client, Safe deployment, heartbeat loop, Redis and Postgres schema, basic JSON logging | 1.5 weeks |
| 2 | Data Plane | Market Stream Gateway, User Stream Gateway, Book State Store, WS reconnection, `accepting_orders`/`seconds_delay`/`negRisk`/`game_start_time` extraction | 1 week |
| 3 | Capability Enricher and Fee Engine | Capability Enricher (fee rate via `/fee-rate` endpoint or SDK auto-handling, tick size, negRisk, rewards, `seconds_delay`), dynamic spread thresholds with bps-to-decimal conversion, `feeRateBps` EIP-712 embedding, fee cache with miss/outage handling | 1 week |
| 4 | Strategy Engine | Quote Engine with Desired/Live/Confirmed order state model; Strategy A (event-driven MM with GTD/GTC logic, inventory skew, sports adapter); Strategy B (penny scanner); Strategy C (resolution sniping with operating window); Order Diff and Execution Actor; all dry-run | 2 weeks |
| 5 | Market Ranker and Control Plane | Universe Scanner with resolution confirmation loop, Market Ranker with EV model, Sports Market Adapter, Parameter Service | 1 week |
| 6 | Risk, Ledgers, and Observability | Risk Gate, kill switch, inventory halt/resume, Order/Fill/Position/Reward/Rebate Ledgers, Recovery Coordinator, Telegram alerting, Prometheus metrics export, daily summary | 1 week |
| 7 | Validation | 24-hour latency shadow run (prerequisite gate); 14-day logic/system paper trading (Step 2); live ramp with Strategy A markout gate before scaling (Step 3) | 2.5 weeks + ramp |
| 8 | Live Deployment | VPS setup, PM2 configuration, Redis and Postgres provisioning, go-live at $500 with ramp config, 14-day monitored ramp | 0.5 weeks |
| **Total** | | | **~11.5 weeks** |

---

## 11. Testing Strategy

### 11.1 Unit Testing

- **Strategy A:** Signal generation, spread threshold with dynamic fee, GTD/GTC selection logic, quote skewing at boundary inventory ratios
- **Strategy B:** Market selection criteria, budget enforcement
- **Strategy C:** Probability gate (both YES and NO sides), fee gate, operating window boundaries, order size scaling formula at threshold/midpoint/ceiling, dynamic offset formula at minimum spread, maximum spread, and mid-spread
- **Fee Calculator:** bps-to-decimal conversion, threshold formula accuracy, cache hit/miss, sustained-miss outage handling, fill-event re-fetch trigger, deviation-triggered invalidation at `FEE_DEVIATION_THRESHOLD_PCT` boundary
- **Capability Enricher:** Correct field extraction and model construction per market type
- **Order Diff Actor:** Correct minimum mutation set (place/cancel/replace) given Desired vs. Confirmed states; self-cross detection — own opposing order within 1 tick triggers cancel-before-place; retry policy — three attempts at 10ms/25ms/50ms backoff, then force-reconciliation; confirm-cancel mode activation and deactivation at `CANCEL_CONFIRM_THRESHOLD_PCT`
- **Book State Store:** Periodic resync fires after `BOOK_RESYNC_INTERVAL_S`; immediate resync after `BOOK_RESYNC_DELTA_THRESHOLD` missed deltas; local book atomically replaced on resync; no new placements during `resyncing = true`; escalation condition (mid move, spread width, gap duration) triggers immediate cancel; quote recompute fires on resync completion without waiting for WS event
- **Sports Market Adapter:** GTD expiry before `game_start_time`, quote cancellation trigger at game start
- **Inventory Manager:** Value-weighted skew formula (`YES_value = YES_shares × YES_price`, `NO_value = NO_shares × (1 - YES_price)`) at boundary ratios; offset direction correctness for YES-overweight and NO-overweight; halt trigger; resume at `INVENTORY_RESUME_THRESHOLD`
- **Risk Gate:** Limit enforcement at boundaries, kill switch, daily counter reset at UTC midnight

Target: > 90% code coverage on Strategy Engine, Fee Calculator, Capability Enricher, Order Diff Actor, and Risk Gate.

### 11.2 Integration Testing

- **Full pipeline:** Universe Scanner → Capability Enricher → Market Ranker → Quote Engine → Order Diff → Risk Gate → mock CLOB
- **Order state reconciliation:** Simulate WS disconnect → verify the open-orders query rebuilds Confirmed state correctly; verify Desired vs. Confirmed diff produces correct mutation set on resume
- **Fee cache miss chain:** Single miss → defer; 5 consecutive misses → cancel quotes, exclude market, alert; cache warm → resume
- **Kill switch + in-flight redemption:** In-flight redemption allowed to complete; queued redemption logs `redemption_interrupted` and alerts
- **Auto-redemption contract call:** Simulate resolution → verify `redeemPositions()` called with correct four arguments (collateral, `parentCollectionId`, `conditionId`, `indexSets` bitmask) → within 1 hour
- **Relayer failover:** Simulate Relayer unreachable for `EOA_FALLBACK_TIMEOUT_S` → verify switch to EOA, alert, reversion on recovery
- **Sports adapter:** Simulate `game_start_time` crossing → verify all open sports market quotes cancelled; verify GTD orders have correct expiry
- **negRisk propagation (FR-117):** negRisk-flagged market → `negRisk = true` in payload; Strategy C excludes market
- **seconds_delay (FR-118):** Market with `seconds_delay > 0` → no order submitted; resumes after delay clears
- **Inventory halt/resume (FR-306):** Value-weighted skew drives market to halt threshold → halt and alert; value-weighted skew recovers → resume
- **Cancel retry policy:** Simulate duplicate-ID rejection → verify exponential backoff (10ms/25ms/50ms); verify force-reconciliation and cycle skip after 3 failures
- **Adaptive confirm-cancel mode:** Simulate rejection rate exceeding `CANCEL_CONFIRM_THRESHOLD_PCT` → verify switch to confirm-cancel mode; simulate rate dropping below threshold → verify reversion to fire-and-forget
- **Fee fill-event invalidation:** Simulate fill on a market → verify fee re-fetch triggered immediately; simulate post-fetch deviation > `FEE_DEVIATION_THRESHOLD_PCT` → verify cache invalidated and quotes re-evaluated
- **Book resync — periodic, stable market:** Advance mock time by `BOOK_RESYNC_INTERVAL_S` with stable mid → verify REST resync, book replaced, no cancels issued, immediate quote recompute fires
- **Book resync — delta threshold:** Simulate `BOOK_RESYNC_DELTA_THRESHOLD` consecutive missed WS deltas → verify immediate resync triggered
- **Book resync — escalation, mid move:** Simulate mid movement exceeding `BOOK_RESYNC_CANCEL_MID_PCT` at resync start → verify all active quotes cancelled before REST call completes
- **Book resync — escalation, spread width:** Simulate spread exceeding `BOOK_RESYNC_CANCEL_SPREAD_TICKS` → verify immediate cancel
- **Book resync — escalation, gap duration:** Simulate WS gap exceeding `BOOK_RESYNC_CANCEL_GAP_MS` → verify immediate cancel
- **Self-cross prevention (FR-210a):** Place bid within 1 tick of own resting ask → verify opposing order cancelled before new order placed

### 11.3 Pre-Validation Smoke Test

Place a single $0.01 Post-Only GTC limit order on a live low-volume market with correct `feeRateBps`, then immediately cancel it. Validates the complete live pipeline end-to-end. Do not proceed to the shadow run if this fails.

### 11.4 Validation — Shadow Run and Paper Trading

**Step 1 — Latency shadow run (prerequisite gate):**
Run for 24 hours with `DRY_RUN=false` and `MAX_TOTAL_EXPOSURE=0`. The Risk Gate cancels every order immediately after signing and dispatch. This exercises the full network round-trip without financial exposure. P95 cancel/replace latency must be < 100ms before proceeding. `DRY_RUN=true` cannot substitute — it skips API calls and measures only local computation (~5ms), providing no signal about production network latency.

**Step 2 — 14-day logic and system validation:**
Run with `DRY_RUN=true`. All of the following must pass:

1. **Simulated cumulative P&L positive for at least 10 of 14 days.** This criterion validates system logic, not adversarial robustness. It will detect bugs (e.g., wrong-side orders, incorrect spread math, fee miscalculation) but cannot detect adverse selection — paper trading fills at the quoted price; live fills come from informed counterparties. This criterion is a necessary logic gate, not a live-readiness gate for Strategy A.
2. Simulated maximum drawdown must not exceed 5% of hypothetical starting capital
3. Simulated trade count must average > 100 per day
4. Fee rate cache hit ratio must exceed 95%
5. Zero inventory halt events during the first 7 days
6. Resolution watchlist must correctly flag > 95% of markets that resolve during the period
7. Auto-redemption must successfully simulate redemption of at least 3 resolved markets
8. Order scoring status must be trackable for at least 5 reward-eligible markets

If any criterion fails, diagnose, fix, and restart Step 2 from day 1.

**Step 3 — Strategy A live markout gate (prerequisite for scaling):**
Paper trading cannot assess adverse selection — the primary live risk for a market-making strategy. Before scaling beyond the initial $500 ramp capital, Strategy A must pass a markout validation gate using real fills:

Run Strategy A live at ramp capital (`MAX_TOTAL_EXPOSURE=400`) for a minimum of 7 live trading days. For each fill, record the mid-price at fill time and again 30 seconds later. Compute **30-second markout** = mid move against the filled side (positive = adverse, negative = favorable).

**Markout gate — ALL must pass over the 7-day window:**
- Median 30-second markout must be ≤ +0.5¢ (i.e., the mid moves less than half a tick against the fill on the median trade)
- Percentage of fills with markout > +1¢ must be < 30%
- Strategy A gross spread capture must exceed the total adverse markout cost (net markout P&L must be positive)

If the markout gate fails, the system is experiencing meaningful adverse selection. Do not scale capital. Instead: review Market Ranker EV estimates for affected markets, tighten `MM_BASE_SPREAD`, or exclude the highest-markout market categories before retesting. The markout gate replaces simulated P&L as the primary live-readiness criterion for Strategy A.

The 30-second markout data shall be recorded in the Fill and Position Ledger and reported in the weekly ongoing validation review.

### 11.5 Live Deployment Ramp

Deploy with $500 capital using the following ramp configuration. **Strategy B and C are disabled for the first 14 live days** — this is the v1 scope guidance from §1. Only Strategy A runs during the ramp period.

```
STRATEGY_A_ENABLED=true
STRATEGY_B_ENABLED=false
STRATEGY_C_ENABLED=false
STRATEGY_A_UNIVERSE_TAGS=["crypto"]   # or ["sports","esports"] — pick one
MM_ORDER_SIZE=5
MAX_TOTAL_EXPOSURE=400
```

Monitor daily for 14 days. Pass the markout gate (§11.4 Step 3) before proceeding. Scale to $2,000 and enable Strategy C after the markout gate passes. Enable Strategy B at the same time or defer to v1.1 — it has no markout gate dependency but adds operational surface during an already active validation period.

### 11.6 Ongoing Validation

- **Weekly:** Compare P&L to paper-trading projections. Investigate deviations > 20%. Review P95 latency trend. Check reward scoring success rate. **Review 30-second markout distribution for Strategy A** — median markout and percentage of fills > +1¢. Rising adverse selection is the earliest warning sign before P&L deteriorates.
- **Monthly:** Reassess `MM_BASE_SPREAD`, `PENNY_MAX_PRICE`, `SNIPE_PROB_THRESHOLD`. Monitor for new fee-eligible market categories — the fee regime is actively expanding and static category lists will become stale.
- **Quarterly:** Evaluate each strategy's positive expected value; disable underperformers. Monitor Polymarket's changelog, Twitter, Discord, and Builder Program announcements for unannounced rule changes.

---

## 12. Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Polymarket makes unannounced rule changes | Critical | Monitor changelog, Twitter, Discord, and Builder Program daily. `STRATEGY_*_ENABLED` flags allow instant disable without restart. Full smoke test suite on every deployment |
| Private key compromise | Critical | Dedicated wallet; env-var only; SSH-only VPS; Redis and Postgres access restricted; Builder credentials server-side only |
| Fee regime expands to new market types | High | Runtime capability discovery (P2) means the system reads `feesEnabled` and `fee_rate_bps` per market dynamically — no code change required for new fee-eligible categories |
| Cancel/replace latency exceeds 100ms | High | Prometheus P95 alert at 80ms (warning) and `LATENCY_ALERT_P95_MS` (halt). Auto-reduces subscriptions. Operator alert |
| Stale quotes during resolution | High | Resolution watchlist with 2-hour entry ban and 30-minute force-cancel. 60-second safety-net stale cancel |
| Silent session disconnect | High | Heartbeat detects dead sessions within 2 cycles. Confirmed state rebuilt via open-orders query on every reconnect |
| Sports market order not cancelled at game start | High | Sports Market Adapter handles game-start cancellation. GTD orders bounded before `game_start_time` |
| Fee cache sustained outage | Medium | After `FEE_CONSECUTIVE_MISS_THRESHOLD` misses, cancel quotes and exclude market. Resume on cache warm |
| Builder Relayer downtime | Medium | EOA failover via FR-216 |
| Inventory skew accumulation | Medium | Active skew adjustment at `INVENTORY_SKEW_THRESHOLD`. Hard halt and alert at `INVENTORY_HALT_THRESHOLD` |
| Reward scoring failures | Medium | FR-158 monitors scoring status. Daily summary reports scoring success rate. Unscored orders flagged |
| Postgres/Redis outage | Medium | Explicit degraded-mode behavior per FR-504a: Redis outage → quoting continues in-memory, alert, halt after `REDIS_OUTAGE_HALT_S`; Postgres outage → fills buffered in-process, quoting continues; both unavailable → safe mode, cancel all orders, halt until recovery and reconciliation complete |
| Black swan correlated resolution losses | Medium | Per-market limits; inventory halt; drawdown kill switch; 20% capital reserve always undeployed |
| Maker Rebate or Liquidity Reward program changes | Low | Both treated as bonus revenue. All three strategies viable without them |

---

## 13. Success Metrics

| KPI | Target | Measurement |
|-----|--------|-------------|
| Net daily profit | > $192/day avg (aspiration) | Aspirational for full ramp. **Not an early-deployment gate.** The primary Strategy A readiness signal is the markout gate (§11.4 Step 3). P&L targets are meaningful only after the markout gate is passed and capital is scaled |
| Strategy A win rate | > 60% | Trades where both bid and ask fill before cancellation |
| Strategy A median 30s markout | ≤ +0.5¢ | Median mid movement against filled side, 30 seconds post-fill. Primary adverse selection signal |
| Strategy A fills with markout > +1¢ | < 30% | Percentage of fills with significant adverse movement within 30 seconds |
| Strategy B hit rate | > 2% | Penny trades returning > 10× cost |
| Strategy C fill rate | > 40% *(provisional — calibrate during paper trading)* | Resolution snipe orders that fill before expiry |
| P95 cancel/replace latency | < 100ms | Prometheus histogram; per trading day |
| Max drawdown | < 5% of capital | |
| Liquidity Rewards/day | > $10 | |
| Maker Rebates/day | > $15 | |
| Maker-to-taker ratio (A+C) | > 98% | Strategy B excluded |
| Order scoring success rate | > 90% | Reward-eligible orders confirmed scoring via order scoring status |
| Fee cache hit ratio | > 95% | |
| Inventory balance | < 65:35 | Max YES/NO ratio before skewing activates |
| Resolution-safe exits | 100% | All quotes pulled before resolution |
| Auto-redemption success rate | > 99% | |
| Session reconnects | < 5/day | |
| Uptime | > 99.5% | |

---

## 14. v4 Roadmap

| Priority | Feature | Expected Impact | Dependency |
|----------|---------|----------------|------------|
| P0 | NegRisk multi-outcome arbitrage | Highest-impact structural arb on the platform | Working binary market infrastructure from v3 |
| P0 | Toxic flow protection | Reduces adverse selection by est. 10–20% | 30+ days of live fill data to calibrate |
| P1 | Cross-platform arbitrage (Polymarket vs. Kalshi) | Captures price dislocations between platforms | Research phase required |
| P1 | Grafana dashboard | Professional operational monitoring; backfillable from v3 Prometheus metrics | Prometheus from v3 (FR-605) |
| P2 | Sponsored market detection | Incremental reward income | Working LR scanner from v3 |
| P3 | Full crash recovery with `--reset` flag | Automated state validation on startup | v3 ledger infrastructure |
| P3 | Holding rewards P&L tracking | Automated attribution of the passive 4% APY | Manual spreadsheet adequate until v4 |
| v5 | Cross-market correlated arbitrage | Information arb and hedge construction | Research phase required |

---

## 15. Glossary

| Term | Definition |
|------|-----------|
| CLOB | Central Limit Order Book. Polymarket's hybrid off-chain matching, on-chain settlement order system |
| Gamma API | Polymarket's market metadata API for events, markets, tags, rewards, and fee parameters |
| CTF | Conditional Token Framework. Gnosis ERC-1155 smart contract standard for outcome tokens |
| Condition ID | Unique market identifier mapping to a smart contract condition |
| Token ID | Unique identifier for a specific outcome share (YES or NO) within a market |
| `feeRateBps` | Fee rate in basis points (1 bps = 0.01%). Required in every EIP-712 signed order payload on fee-eligible markets. Must be fetched dynamically from the API |
| Dynamic taker fee | Per-trade fee on fee-eligible markets. Formula: `C × feeRate × p × (1 - p)`. Peaks at `p = 0.50` and approaches zero at extremes. The actual peak is market-specific depending on `feeRate` — always read from `/fee-rate/{token_id}` at runtime |
| negRisk | Negative risk. Multi-outcome mechanism where only one outcome wins. Strategy C excludes negRisk markets |
| Tick size | Minimum price increment. $0.01 (2dp) standard; $0.001 (3dp) for p < 0.04 or p > 0.96 |
| FOK | Fill-or-Kill. Fills entirely or cancels immediately. Reserved for inventory rebalancing in v3 |
| FAK | Fill-and-Kill. Fills as many shares as possible immediately; remaining portion cancelled. Reserved for inventory rebalancing |
| GTC | Good-Til-Cancelled. Order stays active indefinitely until filled or cancelled. Used for open-horizon markets |
| GTD | Good-Til-Date/Time. Order expires at a specified timestamp. The platform applies a 1-minute security threshold: effective lifetime = specified `expiration` minus 60 seconds. Always compute `expiration` as `target_cutoff_unix - buffer + 60` to achieve the intended effective duration. Preferred for time-bounded exposure and markets with known event horizons |
| Post-Only | Order flag ensuring execution as maker only. Rejects rather than crossing the spread |
| EIP-712 | Ethereum typed structured data signing standard. Used for order signing and credential derivation |
| HMAC-SHA256 | Hash-based message authentication code. Used for Level 2 API authentication |
| Gnosis Safe | 1-of-1 multisig smart contract wallet. Polymarket's proxy wallet for gasless execution |
| Builder Relayer | Polymarket's gasless transaction relay service. Requires Builder Program enrollment |
| Polygon PoS | Layer 2 blockchain (Chain ID 137) hosting Polymarket's smart contracts |
| USDC.e | Bridged USD Coin on Polygon. Settlement currency for all Polymarket trades |
| UMA Oracle | Optimistic Oracle determining market outcomes via propose-dispute mechanism |
| Kill Switch | Emergency mechanism cancelling all open orders and halting trading |
| Adjusted Midpoint | Order book midpoint after dust filtering. Used by Liquidity Rewards formula |
| Barbell Strategy | Portfolio combining stable income strategies (market making, resolution sniping) with high-variance strategies (penny scooping) |
| Desired Orders | What the Quote Engine currently wants resting on the CLOB |
| Live Orders | What the system currently believes is active on the CLOB |
| Confirmed Orders | What has been acknowledged by the exchange via User WS or reconciliation APIs |
| Execution Plane | Latency-sensitive modules: Market/User WS Gateways, Book State Store, Quote Engine, Order Diff, Execution Actor, Liveness Manager, Risk Gate |
| Control Plane | Slower modules: Universe Scanner, Capability Enricher, Market Ranker, Sports Adapter, Parameter Service, Analytics Service |
| Ledger and Reconciliation Plane | Persistent modules: Order Ledger, Fill/Position Ledger, Reward/Rebate Ledger, Recovery Coordinator |
| Sports Market Adapter | Module isolating sports-specific execution rules: game-start cancellation, GTD bounded before game start, 3-second marketable-order delay |
| Runtime capability discovery | Principle: system resolves fee rates, tick sizes, reward configs, and market rules from live API calls rather than static assumptions |
| Holding Rewards | Polymarket's passive yield program (~4% APY on deposited USDC.e). Tracked manually in v3; automated attribution in v4 |
| asyncio | Python async I/O framework. Core concurrency model |
| uvloop | High-performance event loop for Python asyncio. 2–4× throughput over default. Linux/macOS only |
| Cancel/replace loop | Core execution cycle: receive WS event → diff Desired vs. Confirmed → batch cancel stale → batch place updated |

---

## 16. Document History

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 3.14 | 2026-04-08 | Claude | Four cleanup fixes. (1) Stale 1.56% fee peak figure removed from three locations (§3.3 bullet, §8.5 rate limits table, §15 glossary) — the updated formula `C × feeRate × p × (1-p)` does not produce a fixed peak; all three now say the peak is market-specific and must be read from the API. (2) `REQUEST_TIMEOUT_S` (default: 10s) added to §9 config table — was referenced in executor spec but absent from config. (3) FR-158 updated to specify per-order query mechanic: `GET /order-scoring?order_id={id}` must be called per active order, not as a bulk status check. (4) Stale `see §3.1 for live fee schedule` cross-reference in §8.5 removed — §3.1's fee table is now labelled historical. |
| 3.13 | 2026-04-08 | Claude | Three final correctness fixes. `/reward-percentages` → `/rewards/user/percentages`. Order scoring wording tightened to per-order semantics. `STRATEGY_A_UNIVERSE_TAGS` standardised to JSON array. |
| 3.12 | 2026-04-08 | Claude | Four factual and consistency fixes. Fee formula corrected. `/order-scoring-status` → `/order-scoring`. `STRATEGY_A_UNIVERSE_TAGS` added to config table. §11.5 ramp made Strategy A only. |
| 3.11 | 2026-04-08 | Claude | Five structural improvements. (1) v1 deployment scope recommendation added to §1. (2) FR-401 rewritten: Gamma candidate discovery only. (3) Strategy C GTC → GTD throughout. (4) Financial targets reframed as business aspirations. (5) Field naming casing rules added to P2. |
| 3.10 | 2026-04-08 | Claude | Five platform-alignment updates. (1) Fee table labelled as historical snapshot (March 26); March 31 REST `feeSchedule` update noted; runtime discovery (P2) is the only authoritative source. (2) Dedicated rewards endpoints made primary source of truth; §8.3a added with four canonical endpoints; Gamma `markets?rewards=true` repositioned as supplementary. (3) Field naming casing rules added to P2. (4) Latency framing softened in §3.2 and FR-207: < 100ms is a desk target under normal conditions; Cloudflare throttling, exchange pauses, and matching-engine restarts are documented platform-side conditions that can exceed any internal target. (5) Periodic book resync interval changed from 10s to 60s default; desync-triggered resync (BOOK_RESYNC_DELTA_THRESHOLD) is primary mechanism; periodic baseline is last-resort catch. |
| 3.9 | 2026-03-26 | Claude | Three final polish fixes. (1) Pre-resync in-flight orders: orders submitted before `resyncing = true` are now explicitly defined as valid intents — their acknowledgements update Confirmed state normally but the confirmed orders are not re-evaluated against Desired state until post-resync recompute completes, preventing stale Desired state from immediately cancelling well-positioned orders. (2) Market Ranker capital allocation made explicit: selects top N markets by EV (bounded by `MM_MAX_MARKETS`), distributes exposure proportionally to normalised EV scores, excludes EV ≤ 0 markets, guarantees each selected market at least `MM_MIN_ORDER_SIZE` per side, remaining budget allocated pro-rata. (3) Terminology standardised: "active markets" in FR-602 replaced with "active subscriptions" to match usage in Market Stream Gateway and NFR-104; "resolution watchlist" preserved as a distinct concept. |
| 3.8 | 2026-03-26 | Claude | Three final gap closures. Cold-start EV model made explicit. In-flight order acknowledgements during resync clarified. Postgres schema versioning rule added to FR-503. |
| 3.7 | 2026-03-26 | Claude | Book resync quoting policy upgraded to conditional cancel. During resync: no new placements; cancels permitted and required if escalation condition met. Escalation triggers on mid move > `BOOK_RESYNC_CANCEL_MID_PCT`, spread > `BOOK_RESYNC_CANCEL_SPREAD_TICKS`, or WS gap > `BOOK_RESYNC_CANCEL_GAP_MS`. Post-resync: atomic book replacement, immediate quote recompute. Added three config params and five integration test cases. |
| 3.5 | 2026-03-26 | Claude | Nine execution and logic improvements. Critical: (1) Cancel-before-place retry policy expanded to 3-attempt exponential backoff (10ms/25ms/50ms), force-reconciliation after final failure, and adaptive confirm-cancel mode that activates when duplicate-ID rejection rate exceeds `CANCEL_CONFIRM_THRESHOLD_PCT` over 60s. (2) Fee cache hardened with fill-event re-fetch (on every fill) and deviation-triggered invalidation when fee changes by > `FEE_DEVIATION_THRESHOLD_PCT`; FR-151 and FR-156 updated. Medium: (3) Book State Store periodic resync added to FR-106 — REST resync every `BOOK_RESYNC_INTERVAL_S` and after `BOOK_RESYNC_DELTA_THRESHOLD` missed WS deltas. (4) Inventory skew formula replaced with value-weighted skew (`YES_value = YES_shares × YES_price`) in §5.1.5 and FR-306. (5) Strategy C quote offset made dynamic — `max(0.01, min(0.02, spread × 0.5))` — replacing hardcoded 0.02; §5.3.1 and §5.3.3 updated. (6) Self-cross prevention added as FR-210a — opposing own order within 1 tick cancelled before new placement. Minor: (7) GTD adaptive offset logging note added to FR-201. (8) NFR-101 annotated as "measured, not guaranteed" with infrastructure failure framing. (9) FR-453 updated to require per-fill actual maker/taker classification from User channel events rather than inference from order type. New config params: `FEE_DEVIATION_THRESHOLD_PCT`, `BOOK_RESYNC_INTERVAL_S`, `BOOK_RESYNC_DELTA_THRESHOLD`, `CANCEL_CONFIRM_THRESHOLD_PCT`. Unit test spec and integration test list updated for all new logic. |
| 3.4 | 2026-03-26 | Claude | Sports channel WebSocket heartbeat made conditional in FR-501, FR-114, and Liveness Manager — only required if Sports WebSocket channel is opened; v3 sports adapter does not require it. |
| 3.3 | 2026-03-26 | Claude | WebSocket heartbeat wording corrected: "client-side ping frames" replaced with accurate application-level message description. Market/User channels: client sends literal `PING` every 10 seconds, server responds `PONG`. Sports channel: server sends `ping` every 5 seconds, client replies `pong` within 10 seconds. `getServerTime()` method reference neutralised to "server-time endpoint." |
| 3.2 | 2026-03-26 | Claude | Final correctness pass — five edits. FR-157 rewritten to source reward data from Gamma API and dedicated rewards endpoints. FR-451 simplified to `feesEnabled` as sole authoritative switch; `taker_base_fee` removed. Two heartbeat loops made explicit (order-safety POST and WebSocket ping/pong). Typo "Unscorecoded" fixed. SDK method naming standardised to `get_open_orders()` throughout. |
| 3.1 | 2026-03-26 | Claude | Four correctness fixes. Heartbeat interval corrected to 5s. Fee rate lookup corrected to `/fee-rate` endpoint or SDK auto-handling; removed false `get_order_book()` → `feeRateBps` claim. Cancel-before-place sequencing labeled as implementation assumption. GTD +60s security threshold added throughout. |
| 3.0 | 2026-03-26 | Claude | Architectural merge of v2.2 implementation specifics with new three-plane architecture. Replaced five-layer diagram with Execution/Control/Ledger model. Added Design Principles P1–P5 including runtime capability discovery. Added Desired/Live/Confirmed order state model. Upgraded storage to Redis + Postgres. Added Sports Market Adapter (FR-119). Added GTD/GTC guidance. Added reward optimization feedback loop (FR-157, FR-158). Added Market Ranker with EV model. Rewrote FR-451 to use live fee metadata. Reframed 500ms delay claim as community-reported and unverified. Added GTD buffer config params. All v2.2 implementation specifics preserved. |
| 2.2 | 2026-03-25 | Claude | Implementation audit pass — 30 fixes. Corrected fee rate lookup path; fixed CTF `redeemPositions()` to full 4-argument signature; added bps-to-decimal conversion to FR-153; replaced paper trading latency criterion with 24-hour shadow run; specified fire-and-forget cancel/place; defined Strategy C GTC order fate at watchlist windows; added inventory skew formula and Strategy C size scaling; scoped maker ratio KPI to A+C; added strategy enable/disable flags; added Sports adapter note; documented Gamma API reward field names; assigned resolution polling to Market Scanner. |
| 2.1 | 2026-03-25 | Claude | Structural audit pass — 28 fixes. Fixed factual error re maker fill speed; deduplicated FR-114/FR-501; added FR-117 (negRisk), FR-118 (seconds_delay); fully specified FR-215 with retry logic; added minimum order size validation; renamed SNIPE_MIN_PROB → SNIPE_PROB_THRESHOLD; documented 4-to-2-hour Strategy C window; defined "on wire"; clarified FR-153/154 precedence. |
| 2.0 | 2026-03-24 | Claude | Ground-up rewrite from TypeScript to Python/asyncio. Event-driven sub-100ms loop. Dynamic fee engine with feeRateBps embedding. Replaced binary arbitrage with resolution sniping. Promoted inventory skew and auto-redemption. Added platform rule changes section. |
| 1.0 | 2026-03-14 | — | Initial PRD. TypeScript/Node.js. Timer-based polling. Missing feeRateBps. Static fee categories. Both omissions fatal to live operation. |

---

*END OF DOCUMENT*
