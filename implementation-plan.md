# Polymarket Trading Bot — Implementation Plan v2
## Aligned with PRD v3.14

Feed each step to an LLM in order. Do not skip steps. Every step specifies exactly
what to build, what files to create, what tests to write, and the acceptance gate
that must pass before the next step begins. Each step assumes all prior steps pass.

---

## Conventions

- Project root: `polymarket-bot/`
- All paths relative to project root
- Python 3.11+, asyncio + uvloop (Linux/macOS; use default asyncio on Windows)
- All pinned versions verified against PyPI before Step 0 is run
- Acceptance gate = `pytest -x` passes on specified test files
- Implementation is developed under dry-run assumptions through Step 13 —
  no live API calls are made during the build and unit/integration test phases.
  Step 14 introduces live validation scripts (`smoke_test.py`, `shadow_run.py`)
  that intentionally hit the live API. Steps 14–15 are the first points where
  a funded wallet and real `.env` credentials are required.

---

## Step 0 — Project Scaffold and Settings

**PRD refs:** §4.9, §9

**What to build:** Complete project skeleton, dependency pins, and settings model.
No business logic. Every subsequent step imports from this foundation.

**Files to create:**
```
pyproject.toml
.env.example
.gitignore
Makefile
config/__init__.py
config/settings.py
config/logging.py
core/__init__.py
core/execution/__init__.py
core/control/__init__.py
core/ledger/__init__.py
strategies/__init__.py
fees/__init__.py
inventory/__init__.py
auth/__init__.py
storage/__init__.py
alerts/__init__.py
metrics/__init__.py
scripts/.gitkeep
tests/__init__.py
tests/unit/__init__.py
tests/integration/__init__.py
tests/conftest.py
tests/unit/test_settings.py
```

**`pyproject.toml` — exact pins (verify against PyPI before running):**
```toml
[tool.poetry.dependencies]
python = "^3.11"
py-clob-client = "==0.20.0"
web3 = "==6.14.0"
eth-account = "==0.11.2"
websockets = "==12.0"
uvloop = "==0.19.0"
redis = "==5.0.4"
asyncpg = "==0.29.0"
prometheus-client = "==0.20.0"
cryptography = "==42.0.5"
pydantic-settings = "==2.2.1"
httpx = "==0.27.0"

[tool.poetry.dev-dependencies]
pytest = "==8.1.1"
pytest-asyncio = "==0.23.6"
pytest-cov = "==5.0.0"
ruff = "==0.4.1"
mypy = "==1.9.0"
```

**`config/settings.py`** — Pydantic `BaseSettings`. Every field must have the exact
name, default, and description from PRD §9. Required fields with no default
(`PRIVATE_KEY`, `POLYGON_RPC_URL`, `BUILDER_API_KEY`, `BUILDER_SECRET`,
`BUILDER_PASSPHRASE`) must raise `ValidationError` if absent.

Full field list (implement ALL from PRD §9):
```python
# Operational
DRY_RUN: bool = True
USE_RELAYER: bool = True
POLYGON_RPC_URL: str          # required
PRIVATE_KEY: str              # required
BUILDER_API_KEY: str          # required
BUILDER_SECRET: str           # required
BUILDER_PASSPHRASE: str       # required
REDIS_URL: str = "redis://localhost:6379"
DATABASE_URL: str = ""        # required for production
STATE_FILE_PATH: str = "./state.json.enc"  # fallback for minimal deployments
STATE_ENCRYPTION_KEY: str = ""             # Fernet key; required when using fallback

# Strategy enablement
STRATEGY_A_ENABLED: bool = True
STRATEGY_B_ENABLED: bool = True
STRATEGY_C_ENABLED: bool = True
STRATEGY_A_UNIVERSE_TAGS: list[str] = []  # JSON array; [] = all tags

# Heartbeat
HEARTBEAT_INTERVAL_MS: int = 5000

# Fee engine
FEE_CACHE_TTL_S: int = 30
FEE_CONSECUTIVE_MISS_THRESHOLD: int = 5
FEE_DEVIATION_THRESHOLD_PCT: float = 10.0

# Book resync
BOOK_RESYNC_INTERVAL_S: int = 60
BOOK_RESYNC_DELTA_THRESHOLD: int = 5
BOOK_RESYNC_CANCEL_MID_PCT: float = 0.5
BOOK_RESYNC_CANCEL_SPREAD_TICKS: int = 10
BOOK_RESYNC_CANCEL_GAP_MS: int = 2000

# Order execution
CANCEL_CONFIRM_THRESHOLD_PCT: float = 5.0
REQUEST_TIMEOUT_S: int = 10

# Market making
MM_BASE_SPREAD: float = 0.04
MM_COST_FLOOR: float = 0.01
MM_ORDER_SIZE: int = 10
MM_MIN_ORDER_SIZE: int = 0
MM_MAX_MARKETS: int = 20

# GTD buffers
GTD_RESOLUTION_BUFFER_MS: int = 7200000
GTD_GAME_START_BUFFER_MS: int = 300000

# Strategy B
PENNY_MIN_PRICE: float = 0.001
PENNY_MAX_PRICE: float = 0.03
PENNY_BUDGET: float = 5.0
PENNY_MAX_TOTAL: float = 200.0

# Strategy C
SNIPE_PROB_THRESHOLD: float = 0.90
SNIPE_MAX_FEE_BPS: int = 5
SNIPE_MIN_SIZE: int = 5
SNIPE_MAX_SIZE: int = 20
SNIPE_MAX_POSITION: float = 50.0

# Inventory
INVENTORY_SKEW_THRESHOLD: float = 0.65
INVENTORY_HALT_THRESHOLD: float = 0.80
INVENTORY_RESUME_THRESHOLD: float = 0.70
INVENTORY_SKEW_MULTIPLIER: int = 3

# Risk
MAX_TOTAL_EXPOSURE: float = 2000.0
MAX_PER_MARKET: float = 100.0
MAX_DAILY_LOSS: float = 500.0
MAX_DRAWDOWN: float = 500.0

# Watchlist
RESOLUTION_WARN_MS: int = 7200000
RESOLUTION_PULL_MS: int = 1800000
STALE_QUOTE_TIMEOUT_S: int = 60

# Infrastructure
SCAN_INTERVAL_MS: int = 300000
REDEMPTION_POLL_INTERVAL_S: int = 60
EOA_FALLBACK_TIMEOUT_S: int = 30
MIN_USDC_BALANCE: float = 100.0
RPC_MAX_LATENCY_MS: int = 100
LATENCY_ALERT_P95_MS: int = 150
REDIS_OUTAGE_HALT_S: int = 300
POSTGRES_BUFFER_MAX_ROWS: int = 10000
PROMETHEUS_PORT: int = 9090
TELEGRAM_WEBHOOK_URL: str = ""
DISCORD_WEBHOOK_URL: str = ""
```

**`config/logging.py`** — Structured JSON formatter. Every entry must include:
`timestamp` (ISO 8601), `level`, `module`, `message`. Additional fields additive.

**`tests/conftest.py`** — Shared fixtures:
- `settings`: `Settings` instance with `DRY_RUN=True` and test-safe defaults
- `mock_clob_client`: `AsyncMock` of `ClobClient`
- `mock_ws_message(token_id, bids, asks)`: factory returning fake WS book dicts

**`tests/unit/test_settings.py`** must verify:
- Every field has the correct default
- `PRIVATE_KEY` absent → `ValidationError`
- `POLYGON_RPC_URL` absent → `ValidationError`
- `STRATEGY_A_UNIVERSE_TAGS` parses JSON array from env string:
  `'["crypto"]'` → `["crypto"]`
- `HEARTBEAT_INTERVAL_MS` default is `5000` (not 30000)
- `BOOK_RESYNC_INTERVAL_S` default is `60` (not 10)

**Acceptance gate:**
```bash
pytest tests/unit/test_settings.py -v
```

---

## Step 1 — Authentication and Wallet

**PRD refs:** §4.10, §4.12, FR-112, FR-204, FR-205, FR-216

**Files to create:**
```
auth/credentials.py
auth/relayer.py
tests/unit/test_credentials.py
tests/unit/test_relayer.py
```

**`auth/credentials.py`:**
```python
async def derive_credentials(private_key: str, host: str, chain_id: int) -> ApiCreds:
    """
    Calls ClobClient.create_or_derive_api_creds() — Level 1 EIP-712 auth.
    Returns (api_key, secret, passphrase).
    Private key is NOT stored after this call — return credentials only.
    All subsequent API calls use Level 2 HMAC-SHA256 with derived credentials.
    """

def build_clob_client(settings: Settings, creds: ApiCreds) -> ClobClient:
    """
    Signature type 2 (Gnosis Safe) when USE_RELAYER=True.
    Signature type 0 (EOA) when USE_RELAYER=False or for local dev.
    """
```

**`auth/relayer.py`:**
```python
async def get_or_deploy_safe(settings: Settings, redis_client) -> str:
    """
    1. Check Redis cache for safe address (key: 'wallet:safe_address')
    2. If absent, call RelayClient.get_deployed()
    3. If not deployed, call RelayClient.deploy()
    4. Cache address in Redis; return it
    """

async def submit_with_failover(order, relayer_client, eoa_client,
                                settings, alerts):
    """
    FR-216: attempt Relayer submission first.
    If Relayer unreachable for EOA_FALLBACK_TIMEOUT_S seconds:
      - switch to EOA (type 0) execution
      - send RELAYER_FAILOVER_ACTIVATED alert
      - revert to Relayer on recovery
      - suspend submissions if POL balance insufficient for gas
    """
```

**`tests/unit/test_credentials.py`** must verify:
- `derive_credentials` returns a 3-tuple `(api_key, secret, passphrase)`
- Private key does not appear anywhere in the returned object
- `build_clob_client` uses signature type 2 when `USE_RELAYER=True`
- `build_clob_client` uses signature type 0 when `USE_RELAYER=False`

**`tests/unit/test_relayer.py`** must verify:
- Redis hit → safe address returned without calling `deploy()`
- Redis miss + `get_deployed()` miss → `deploy()` called; address cached
- Failover: Relayer unreachable for `EOA_FALLBACK_TIMEOUT_S` → EOA activated, alert sent

**Acceptance gate:**
```bash
pytest tests/unit/test_credentials.py tests/unit/test_relayer.py -v
```

---

## Step 2 — Storage Layer

**PRD refs:** §4.8, FR-503, FR-504, FR-504a

**Files to create:**
```
storage/redis_client.py
storage/postgres_client.py
storage/migrations/001_initial_schema.sql
tests/unit/test_storage.py
```

**`storage/redis_client.py`** — async `redis.asyncio` wrapper:
- `get`, `set(key, value, ex=None)`, `delete`, `exists`, `hset`, `hget`
- `health_check() -> bool`
- Raise `StorageError` on `RedisError`; all operations async

**`storage/migrations/001_initial_schema.sql`:**
```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,           -- 'BUY' | 'SELL'
    price NUMERIC(10,4) NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    strategy TEXT NOT NULL,       -- 'A' | 'B' | 'C'
    time_in_force TEXT NOT NULL,  -- 'GTC' | 'GTD'  (time-in-force concept)
    post_only BOOLEAN NOT NULL DEFAULT FALSE,  -- execution constraint (separate from TIF)
    status TEXT NOT NULL,         -- 'SUBMITTED'|'CONFIRMED'|'CANCELLED'|'REJECTED'
    fee_rate_bps INTEGER,
    expiration BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fills (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price NUMERIC(10,4) NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    strategy TEXT NOT NULL,
    maker_taker TEXT NOT NULL,    -- 'MAKER' | 'TAKER' — from User channel, not inferred
    simulated BOOLEAN NOT NULL DEFAULT FALSE,
    mid_at_fill NUMERIC(10,4),
    mid_at_30s NUMERIC(10,4),
    markout_30s NUMERIC(10,4),    -- positive = adverse, negative = favourable
    fill_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    avg_price NUMERIC(10,4) NOT NULL,
    realized_pnl NUMERIC(14,4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rewards (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    date DATE NOT NULL,
    rewards_earned NUMERIC(10,4),
    rebates_earned NUMERIC(10,4),
    scoring_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, date)
);
```

**`storage/postgres_client.py`** — async `asyncpg` wrapper:
- `execute`, `fetch`, `fetchrow`
- `run_migrations(migrations_dir: Path)` — forward-only; skips already-applied
  versions; records each in `schema_migrations`; **never rolls back**
- `health_check() -> bool`
- In-process fill buffer: `asyncio.Queue` capped at `POSTGRES_BUFFER_MAX_ROWS`;
  on overflow log to local fallback file and alert (FR-504a sub-case 2)
- **FR-504a sub-case 3 (both Redis and Postgres unavailable simultaneously):**
  The `PostgresClient` and `RedisClient` must expose a combined health signal.
  When both `health_check()` calls fail, the storage layer sets
  `storage_safe_mode = True`. The orchestrator (Step 12) monitors this flag:
  on `True`, it cancels all open orders, halts new quote placement, sends
  `SAFE_MODE_ENTERED` alert, and waits for at least one tier to recover.
- **FR-504a sub-case 4 (safe mode exit):** When either tier recovers, the
  storage layer clears `storage_safe_mode`. The orchestrator then: calls
  `rebuild_confirmed_state()`, waits for at least one successful durable
  Postgres write, sends `SAFE_MODE_EXITED` alert, then resumes quoting.

**`tests/unit/test_storage.py`** must verify:
- `run_migrations` applies in version order
- `run_migrations` is idempotent — skips already-applied versions
- Fill buffer caps at `POSTGRES_BUFFER_MAX_ROWS`; overflow logged
- `health_check()` returns `False` on connection failure

**Acceptance gate:**
```bash
pytest tests/unit/test_storage.py -v
```

---

## Step 3 — WebSocket Data Plane and Liveness

**PRD refs:** FR-105–109, FR-114, FR-501, §5.1.2 resync policy

**Files to create:**
```
core/execution/types.py
core/execution/market_stream.py
core/execution/user_stream.py
core/execution/book_state.py
core/execution/liveness.py
tests/unit/test_book_state.py
tests/unit/test_liveness.py
```

**`core/execution/types.py`** — shared event and intent types.
Defined here so Steps 6 and 7 can both import without circular dependencies.
All other execution-plane modules import from this file — do not redefine
these types elsewhere.

```python
@dataclass
class PriceLevel:
    price: float
    size: float

@dataclass
class BookEvent:
    token_id: str
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    timestamp: float

@dataclass
class FillEvent:
    order_id: str
    token_id: str
    market_id: str
    side: str           # 'BUY' | 'SELL'
    price: float
    size: float
    maker_taker: str    # 'MAKER' | 'TAKER' — from User channel, not inferred
    strategy: str
    fill_timestamp: float

@dataclass
class CancelEvent:
    order_id: str
    token_id: str

@dataclass
class OrderAckEvent:
    order_id: str
    token_id: str

@dataclass
class Signal:
    """Intermediate signal produced by a strategy's evaluate() method,
    before the Quote Engine has applied reward constraints. Not yet
    validated by the Order Diff or Risk Gate."""
    token_id: str
    side: str
    price: float
    size: float
    time_in_force: str   # 'GTC' | 'GTD'
    post_only: bool
    expiration: int | None
    strategy: str
    fee_rate_bps: int
    neg_risk: bool
    tick_size: float

@dataclass
class OrderIntent:
    """Desired order state passed from QuoteEngine to OrderDiff.
    Identical fields to Signal; separate type so the diff layer has
    a distinct, typed input."""
    token_id: str
    side: str
    price: float
    size: float
    time_in_force: str   # 'GTC' | 'GTD'
    post_only: bool      # True for Strategy A and C; False for Strategy B
    expiration: int | None  # required when time_in_force == 'GTD'
    strategy: str
    fee_rate_bps: int    # normalised from 'base_fee' at fetch boundary
    neg_risk: bool
    tick_size: float
```

**`core/execution/market_stream.py`** — `MarketStreamGateway`:
- Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `subscribe(token_ids)`, `unsubscribe(token_ids)` — no full reconnect required
- Reconnect: exponential backoff 1s → 2s → 4s → … → 30s cap
- Emit `BookEvent` to `asyncio.Queue`
- Sequence gap detection: if message shape exposes a reliable per-token sequence
  field, track it; otherwise detect via ordering anomalies (mid jump >
  `BOOK_RESYNC_CANCEL_MID_PCT`, crossed book). Increment `missed_delta_count`
  per token on gap. When ≥ `BOOK_RESYNC_DELTA_THRESHOLD`, enqueue resync trigger.

**`core/execution/user_stream.py`** — `UserStreamGateway`:
- Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- **Auth is sent in the subscription message body, not as HTTP headers.**
  After the WebSocket connection is established, send the auth payload as the
  first message on the socket:
  ```json
  {
    "auth": {
      "apiKey": "...",
      "secret": "...",
      "passphrase": "..."
    },
    "type": "user"
  }
  ```
  Do not pass auth as HTTP headers on connect — the user channel docs show
  credentials in the subscription message body, not in the handshake headers.
- **After auth succeeds, send the market subscription message.** The user channel
  uses condition IDs (not asset/token IDs) in the subscription request:
  ```json
  {
    "action": "subscribe",
    "markets": ["<condition_id_1>", "<condition_id_2>", ...]
  }
  ```
  The `markets` list must be updated dynamically as the active subscription set
  changes — send a new subscription message whenever markets are added or removed.
- Same reconnect policy as market stream; on reconnect, re-send auth then
  re-subscribe to all current condition IDs
- Emit `FillEvent`, `CancelEvent`, `OrderAckEvent` to separate queues

**`core/execution/book_state.py`** — `BookStateStore` (one per token):
```python
@dataclass
class BookStateStore:
    token_id: str
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    last_update_ts: float
    last_mid: float | None
    resyncing: bool = False
    missed_delta_count: int = 0

def update(self, event: BookEvent) -> None: ...

async def start_resync(self, ws_gap_ms: int, settings: Settings) -> bool:
    """
    Sets resyncing=True.
    Returns True (escalation — caller must cancel all active quotes) if ANY of:
      - mid moved > BOOK_RESYNC_CANCEL_MID_PCT since last WS update
      - spread > BOOK_RESYNC_CANCEL_SPREAD_TICKS ticks
      - ws_gap_ms > BOOK_RESYNC_CANCEL_GAP_MS
    """

async def complete_resync(self, rest_book: dict) -> None:
    """
    Atomically replaces full book from REST response.
    Clears resyncing=False.
    Pre-resync in-flight acks update Confirmed state during window,
    but are NOT re-evaluated against Desired state until this returns.
    """

def best_bid(self) -> float | None: ...
def best_ask(self) -> float | None: ...
def mid(self) -> float | None: ...
def spread_ticks(self, tick_size: float) -> int: ...
```

**`core/execution/liveness.py`** — three independent asyncio tasks (FR-501):

```python
async def order_safety_heartbeat_loop(clob_client, settings, alerts):
    """
    Loop 1 — ORDER SAFETY (production-critical).
    POST heartbeat every HEARTBEAT_INTERVAL_MS (5s default).
    Platform cancels ALL open orders if no heartbeat within 10s.
    On 2 consecutive missed acks: declare session dead, signal reconnect.
    Also queries the server-time endpoint on each heartbeat cycle to detect
    local clock drift. Drift > 5s: log warning. Drift > 30s: send alert
    (large drift causes GTD expiry miscalculation and order rejection).
    MUST start only after CLOB client is authenticated and connected.
    """

async def market_user_ws_heartbeat_loop(market_ws, user_ws):
    """
    Loop 2 — WEBSOCKET KEEPALIVE (Market + User channels).
    Note: PRD FR-501 gives a definitive answer — literal string 'PING' every
    10 seconds, literal 'PONG' in response, application-level message not a
    WebSocket protocol frame. However, the market and user channel reference
    pages in the current docs show 'Ping {}' / 'Pong {}' JSON examples, which
    is inconsistent with FR-501. The implementation plan uses an adapter pattern
    to avoid baking in a format that may be wrong. The PRD's stated format
    ('PING'/'PONG') is the authoritative starting point — start with that and
    confirm during the Step 14 smoke test. If 'PING' is not acknowledged, try
    'Ping {}'. Once confirmed, document and freeze in a code comment.
    MUST start only after stream connections are confirmed established.
    """

async def sports_ws_heartbeat_loop(sports_ws):
    """
    Loop 3 — SPORTS CHANNEL (conditional).
    Only instantiate if sports_ws is not None.
    Direction REVERSED from Loops 1+2:
    Server sends 'ping' every 5s; client must reply 'pong' within 10s.
    This format is unambiguous in the sports channel docs.
    """
```

**Update `tests/conftest.py`** — now that `BookEvent` and `PriceLevel` exist,
update the `mock_ws_message` factory defined in Step 0 to return a proper
`BookEvent` instance instead of a raw dict:
```python
from core.execution.types import BookEvent, PriceLevel

def mock_ws_message(token_id: str,
                    bids: list[tuple[float, float]],
                    asks: list[tuple[float, float]]) -> BookEvent:
    return BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
        timestamp=time.time(),
    )
```
All downstream tests that use this factory will now receive the correct type.

**`tests/unit/test_book_state.py`** must cover:
- `update()` applies incremental bid/ask changes correctly
- `start_resync()` returns `True` for each of the 3 escalation conditions independently
- `start_resync()` returns `False` on stable market
- `complete_resync()` atomically replaces book and clears `resyncing=False`
- Pre-resync ack arrives during resync window → Confirmed state updated →
  re-evaluation blocked until `complete_resync()` called

**`tests/unit/test_liveness.py`** must cover:
- Loop 1 fires at `HEARTBEAT_INTERVAL_MS`; session dead after 2 consecutive missed acks
- Loop 2: `WsHeartbeatAdapter` sends configured message at 10s interval. Write
  two named test cases that make the format ambiguity explicit:
  - `test_heartbeat_format_ping_string`: adapter configured with `'PING'` →
    verify `'PING'` sent; `'PONG'` response clears health flag
    (format from WebSocket overview docs)
  - `test_heartbeat_format_ping_json`: adapter configured with `'Ping {}'` →
    verify `'Ping {}'` sent; `'Pong {}'` response clears health flag
    (format from market/user channel reference docs)
  Both tests must pass. The correct format is confirmed during Step 14 smoke
  test by observing which one the server acknowledges.
- Loop 3: NOT started when `sports_ws=None`
- All three loops are independent — failure in one does not affect others

**Acceptance gate:**
```bash
pytest tests/unit/test_book_state.py tests/unit/test_liveness.py -v
```

---

## Step 4 — Capability Enricher and Fee Engine

**PRD refs:** FR-102, FR-103a, FR-150–158, §3.1, Design Principle P2 field naming

**Files to create:**
```
core/control/capability_enricher.py
fees/calculator.py
fees/cache.py
tests/unit/test_capability_enricher.py
tests/unit/test_fee_calculator.py
tests/unit/test_fee_cache.py
```

**Field naming rule (PRD P2 — critical):**
- Gamma API: camelCase (`acceptingOrders`, `secondsDelay`, `gameStartTime`,
  `negRisk`, `tickSize`, `minimumOrderSize`, `resolutionTime`)
- CLOB order book: snake_case (`tick_size`, `neg_risk`, `min_order_size`)
- EIP-712 payloads: camelCase (`tokenID`, `feeRateBps`, `negRisk`, `tickSize`)
- Internal model: snake_case throughout

The Capability Enricher is the ONLY module that maps raw API fields to internal
model fields. No other module may reference raw API field names directly.

**`core/control/capability_enricher.py`:**
```python
@dataclass
class MarketCapabilityModel:
    token_id: str
    condition_id: str
    tick_size: float
    minimum_order_size: float
    neg_risk: bool
    fees_enabled: bool          # feesEnabled — authoritative eligibility switch
    fee_rate_bps: int           # from /fee-rate/{token_id}, field 'base_fee'
    seconds_delay: int          # from Gamma secondsDelay
    accepting_orders: bool      # from Gamma acceptingOrders
    game_start_time: datetime | None
    resolution_time: datetime | None
    rewards_min_size: float | None
    rewards_max_spread: float | None
    rewards_daily_rate: float | None
    adjusted_midpoint: float | None
    tags: list[str]
    # No prev_* fields here — MarketCapabilityModel represents current market state only.
    # Prior snapshots for mutation detection are stored in UniverseScanner (Step 8),
    # which owns the comparison logic and the snapshot dict keyed by condition_id.

def enrich(raw_market: dict) -> MarketCapabilityModel:
    """Maps Gamma camelCase fields → internal snake_case model."""

def detect_mutations(old: MarketCapabilityModel,
                     new: MarketCapabilityModel) -> list[MutationType]:
    """
    FR-103a — compares two MarketCapabilityModel snapshots (both passed in).
    Called by UniverseScanner, which holds the previous snapshot dict.
    Four mutation types:
    RESOLUTION_TIME_CHANGED
    ACCEPTING_ORDERS_FLIPPED_FALSE
    FEE_RATE_CHANGED
    SECONDS_DELAY_BECAME_NONZERO
    """
```

**`fees/calculator.py`:**
```python
def bps_to_decimal(bps: int) -> float:
    return bps / 10_000

def min_profitable_spread(fee_rate_bps: int, base_spread: float,
                          cost_floor: float) -> float:
    """FR-153: max(base_spread, 2 × fee_rate_decimal + cost_floor)"""

def passes_strategy_a_gate(fee_rate_bps: int, observed_spread: float,
                            base_spread: float, cost_floor: float) -> bool:
    """FR-153 AND FR-154 must both pass (not just one)."""

def passes_strategy_c_gate(fee_rate_bps: int, max_fee_bps: int) -> bool:
    """FR-155: fee_rate_bps < max_fee_bps — hard gate, not <="""
```

**`fees/cache.py`** — `FeeRateCache`:
- `get(token_id) -> int | None`; TTL = `FEE_CACHE_TTL_S`
- `set(token_id, bps)`, `invalidate(token_id)`
- `record_miss(token_id)` → increments consecutive miss counter
- `should_exclude(token_id) -> bool` — True when misses ≥ threshold
- `check_deviation(token_id, new_bps) -> bool` — True when > `FEE_DEVIATION_THRESHOLD_PCT`
- `on_fill(token_id)` — triggers immediate async re-fetch and cache override

**Fee fetch — critical:**
```python
async def fetch_fee_rate(token_id: str, http_client: httpx.AsyncClient,
                         clob_host: str, hmac_headers: dict) -> int:
    """
    Endpoint: GET /fee-rate/{token_id}
    Response field: 'base_fee'  ← NOT 'feeRateBps'
    Always normalise at this boundary:
    """
    r = await http_client.get(f"{clob_host}/fee-rate/{token_id}",
                               headers=hmac_headers)
    r.raise_for_status()
    return r.json()["base_fee"]   # stored internally as fee_rate_bps
```

**`tests/unit/test_fee_calculator.py`** must cover:
- `bps_to_decimal(78) == 0.0078`
- PRD worked example: `fee_rate_bps=78` →
  `min_spread = max(0.04, 2×0.0078+0.01) = max(0.04, 0.0256) = 0.04`
- FR-153 passing alone is insufficient; FR-154 must also pass
- Strategy C gate: `bps=4` passes with `SNIPE_MAX_FEE_BPS=5`; `bps=6` fails

**`tests/unit/test_fee_cache.py`** must cover:
- TTL expiry → `get()` returns None
- 5 consecutive misses → `should_exclude()` True
- New value differs by > 10% → `check_deviation()` True; cache invalidated
- `on_fill()` triggers re-fetch (mock the call; assert called)
- After `invalidate()`, `get()` returns None

**`tests/unit/test_capability_enricher.py`** must cover:
- camelCase → snake_case mapping:
  - `acceptingOrders` → `accepting_orders`
  - `secondsDelay` → `seconds_delay`
  - `gameStartTime` → `game_start_time`
  - `negRisk` → `neg_risk`
  - `tickSize` → `tick_size`
- All four FR-103a mutation types detected correctly when `detect_mutations(old, new)`
  is called with two model snapshots
- `MarketCapabilityModel` has no `prev_*` fields — verify they are absent

**Acceptance gate:**
```bash
pytest tests/unit/test_capability_enricher.py \
       tests/unit/test_fee_calculator.py \
       tests/unit/test_fee_cache.py -v
```

---

## Step 5 — Inventory Manager

**PRD refs:** §5.1.5, FR-306

**Files to create:**
```
inventory/manager.py
tests/unit/test_inventory.py
```

**`inventory/manager.py`:**
```python
@dataclass
class InventoryState:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_price: float = 0.5   # live midpoint from BookStateStore

def value_weighted_skew(state: InventoryState) -> float:
    """
    PRD §5.1.5:
    YES_value = yes_shares × yes_price
    NO_value  = no_shares × (1 - yes_price)
    skew = (YES_value - NO_value) / (YES_value + NO_value)
    Returns 0.0 when total == 0.
    """

def quote_offset_ticks(skew: float, multiplier: int) -> int:
    """round(skew × multiplier). Positive = overweight YES."""

def should_halt(skew: float, halt_threshold: float) -> bool:
    return abs(skew) >= halt_threshold

def should_resume(skew: float, resume_threshold: float) -> bool:
    return abs(skew) < resume_threshold

def apply_fill(state: InventoryState, side: str, size: float) -> InventoryState:
    """Updates yes_shares or no_shares on fill event."""
```

**`tests/unit/test_inventory.py`** must cover:
- `skew == 0` when `YES_value == NO_value`
- **Key correctness test:** 100 YES at `p=0.95` must produce different skew than
  100 YES at `p=0.50`. A share-count formula gives identical results; the
  value-weighted formula must not.
- Halt triggers at `INVENTORY_HALT_THRESHOLD`; resume below `INVENTORY_RESUME_THRESHOLD`
- Overweight YES → positive offset → bid lowered, ask raised
- Overweight NO → negative offset → bid raised, ask lowered
- `apply_fill` updates the correct side

**Acceptance gate:**
```bash
pytest tests/unit/test_inventory.py -v
```

---

## Step 6 — Strategies and Sports Adapter

**PRD refs:** §5.1–5.3, FR-116–119, FR-153–155, FR-210, FR-214

**Files to create:**
```
strategies/base.py
strategies/strategy_a.py
strategies/strategy_b.py
strategies/strategy_c.py
core/execution/quote_engine.py
core/control/sports_adapter.py
tests/unit/test_strategy_a.py
tests/unit/test_strategy_b.py
tests/unit/test_strategy_c.py
tests/unit/test_quote_engine.py
tests/unit/test_sports_adapter.py
```

`Signal` and `OrderIntent` are imported from `core/execution/types`
(defined in Step 3). Do not redefine them here.

The Sports Adapter is built in this step rather than Step 8 because
Strategy A gate 10 calls `should_cancel_at_game_start()` directly.
Moving it here eliminates the forward dependency.

**`strategies/base.py`:**
```python
from core.execution.types import Signal

class BaseStrategy(ABC):
    strategy_id: str         # 'A', 'B', or 'C'
    enabled: bool
    max_exposure: float
    kill_switch_active: bool = False

    @abstractmethod
    async def evaluate(self, market: MarketCapabilityModel,
                       book: BookStateStore,
                       inventory: InventoryState,
                       fee_cache: FeeRateCache) -> list[Signal]: ...

    def kill(self): self.kill_switch_active = True
```

**`strategies/strategy_a.py`** — 10 entry gates (all must pass in order):
1. `STRATEGY_A_ENABLED` and `not kill_switch_active`
2. Market tag in `STRATEGY_A_UNIVERSE_TAGS` (or list is empty)
3. Not within `RESOLUTION_WARN_MS` of resolution
4. `accepting_orders == True`
5. `seconds_delay == 0`
6. `passes_strategy_a_gate(fee_rate_bps, observed_spread, ...)` — FR-153 + FR-154
7. Midpoint between 0.05 and 0.95
8. Current position < `MAX_PER_MARKET`
9. **Post-positioning spread check:** the quoted bid-ask spread after applying tick
   offsets must exceed 3¢. This is distinct from gate 6 (which checks observed market
   spread pre-entry) — this checks that our own quotes still maintain a minimum spread
   after placement. If `ask - bid < 0.03`, skip entry.
10. If this is a sports market (`game_start_time` not null), defer to the Sports
    Market Adapter. Block entry only if the adapter says quoting is unsafe —
    specifically if `should_cancel_at_game_start()` returns True (the cancellation
    window has begun). Do NOT block sports markets categorically; Strategy A may
    quote sports markets with correct GTD expiry set before `game_start_time`.

Quote positioning:
```python
bid = best_bid + tick_size
ask = best_ask - tick_size
# Apply value-weighted skew offset:
bid -= offset_ticks * tick_size
ask += offset_ticks * tick_size
```

GTD/GTC duration selection:
```python
def select_duration(cap: MarketCapabilityModel, settings: Settings):
    """
    Use GTD when market has a known time boundary within 6 hours.
    expiration = target_cutoff_unix - (buffer_ms // 1000) + 60
    (+60 = platform 1-minute security threshold)
    Use GTC for open-horizon markets.
    """
```

**`strategies/strategy_b.py`** — 1-minute polling (not event-driven):
- Entry: price between `PENNY_MIN_PRICE` and `PENNY_MAX_PRICE`
- Resolution ≥ 24h away; market not resolved
- Budget: `PENNY_BUDGET` per trade; `PENNY_MAX_TOTAL` total cap
- **Order type: marketable limit order priced at or above best_ask.**
  Polymarket has no MARKET order primitive — all orders are limit orders.
  Do NOT use a `MARKET` order type. Taker execution uses a limit order
  priced to cross the spread.

**`strategies/strategy_c.py`** — 8 entry gates:
1. `STRATEGY_C_ENABLED` and `not kill_switch_active`
2. `YES_prob > SNIPE_PROB_THRESHOLD` OR `YES_prob < (1 - SNIPE_PROB_THRESHOLD)`
3. At least 4 hours until resolution (outer bound)
4. Not within 2 hours of resolution (inner bound — FR-214)
5. `fee_rate_bps < SNIPE_MAX_FEE_BPS` — hard gate
6. **Bid-ask spread in target direction ≥ 2¢.** If the spread is tighter than 2¢
   the opportunity is too thin to snipe profitably.
7. Position < `SNIPE_MAX_POSITION`
8. NOT `neg_risk` market

Dynamic offset:
```python
def compute_offset(spread: float) -> float:
    return max(0.01, min(0.02, spread * 0.5))
# spread=0.04 → 0.02
# spread=0.015 → max(0.01, 0.0075) = 0.01  ← floor applies
# spread=0.005 → max(0.01, 0.0025) = 0.01  ← floor applies
```

Size scaling:
```python
def compute_size(yes_prob, threshold, min_size, max_size) -> int:
    certainty = yes_prob if yes_prob > threshold else (1 - yes_prob)
    scale = (certainty - threshold) / (1.0 - threshold)
    return min_size + int(scale * (max_size - min_size))
# threshold=0.90, prob=0.95 → certainty=0.95 → 5 + floor(0.5×15) = 12 shares
```

Order type: **GTD, Post-Only**
```python
expiration = resolution_time_unix - (GTD_RESOLUTION_BUFFER_MS // 1000) + 60
# GTD is correct here — the 4-to-2-hour window is time-bounded by definition.
# GTC would leave orders resting if the market extends.
```

**`core/control/sports_adapter.py`:**
```python
def is_sports_market(cap: MarketCapabilityModel) -> bool:
    return cap.game_start_time is not None

def should_cancel_at_game_start(cap: MarketCapabilityModel) -> bool:
    return cap.game_start_time is not None and datetime.utcnow() >= cap.game_start_time

def compute_gtd_before_game_start(cap: MarketCapabilityModel,
                                   settings: Settings) -> int:
    """
    game_start_unix - (GTD_GAME_START_BUFFER_MS // 1000) + 60
    Setting is _MS — always convert with // 1000.
    GTD_GAME_START_BUFFER_S does not exist in settings.
    """

def marketable_order_delay_ms() -> int:
    return 3000   # documented 3s delay for sports marketable orders
```

**`tests/unit/test_strategy_a.py`** must cover:
- Each of the 10 gates independently blocks when condition fails
- Gate 9 (post-positioning spread): `ask - bid < 0.03` → entry blocked
- Bid = `best_bid + tick_size`; ask = `best_ask - tick_size`
- Skew offset direction: overweight YES → bid lowered, ask raised
- GTD selected when within 6h of resolution;
  expiry = `cutoff - (buffer_ms // 1000) + 60`
- GTC when no time boundary
- `STRATEGY_A_UNIVERSE_TAGS=["crypto"]` blocks a non-crypto market

**`tests/unit/test_strategy_b.py`** must cover:
- `price < PENNY_MIN_PRICE` → rejected
- `price > PENNY_MAX_PRICE` → rejected
- Total budget cap enforced; trade blocked when `PENNY_MAX_TOTAL` would be exceeded
- Order placed as a limit order (not a `MARKET` type)

**`tests/unit/test_strategy_c.py`** must cover:
- YES side: `prob=0.91` passes; `prob=0.89` fails (threshold=0.90)
- NO side: `prob=0.09` passes; `prob=0.11` fails
- Fee gate: `bps=4` passes with `SNIPE_MAX_FEE_BPS=5`; `bps=6` fails
- Spread gate: spread=`0.015` → rejected; spread=`0.02` → passes
- negRisk market → always rejected
- 4h outer bound: 5h from resolution → entry; 3h → rejected
- 2h inner bound: 1.5h from resolution → rejected (FR-214)
- Dynamic offset: `spread=0.04` → `0.02`; `spread=0.015` → **`0.01`** (floor)
- Size: `threshold=0.90, prob=0.95` → **12 shares** (verify this exact value)
- Order type is GTD; expiry uses `GTD_RESOLUTION_BUFFER_MS // 1000`

**`tests/unit/test_sports_adapter.py`** must cover:
- `game_start_time=None` → `is_sports_market()` returns False
- `should_cancel_at_game_start()` True at and after game_start_time
- GTD expiry uses `GTD_GAME_START_BUFFER_MS // 1000` (ms→s conversion)
- GTD expiry includes `+60` security offset

**`core/execution/quote_engine.py`** — PRD §4.4.1 Execution Plane module.
Sits between BookStateStore and OrderDiff. Takes live market state and produces
the desired-order list. This is not the strategy itself — it orchestrates the
strategies and applies reward positioning.

```python
class QuoteEngine:
    def __init__(self, strategies: list[BaseStrategy],
                 fee_cache: FeeRateCache,
                 inventory_manager: InventoryManager,
                 settings: Settings): ...

    async def evaluate(self,
                       market: MarketCapabilityModel,
                       book: BookStateStore,
                       inventory: InventoryState) -> list[OrderIntent]:
        """
        Called on every qualifying BookEvent.
        Steps:
        1. Call each enabled strategy's evaluate() in order.
        2. Collect signals; discard from disabled or kill-switched strategies.
        3. Apply reward positioning (FR-402–405): if market is reward-eligible,
           constrain quotes to within rewardsMaxSpread of adjustedMidpoint,
           enforce rewardsMinSize as a floor on order size, and prefer inner
           ticks closest to adjustedMidpoint.
        4. Return list[OrderIntent] representing desired-order state.
        """

    async def re_evaluate(self,
                          market: MarketCapabilityModel,
                          book: BookStateStore,
                          inventory: InventoryState) -> list[OrderIntent]:
        """
        Called on FillEvent after inventory is updated.
        Identical to evaluate() — exists as a named entry point so the
        orchestrator event routing is explicit and traceable.
        """
```

**`tests/unit/test_quote_engine.py`** must cover:
- Disabled strategy signals are excluded from desired-order list
- Kill-switched strategy signals are excluded
- Reward-eligible market: quote constrained within `rewardsMaxSpread` of
  `adjustedMidpoint`; order size floored at `rewardsMinSize`
- Non-reward market: reward constraints not applied
- `re_evaluate()` produces same output shape as `evaluate()`

**Acceptance gate:**
```bash
pytest tests/unit/test_strategy_a.py \
       tests/unit/test_strategy_b.py \
       tests/unit/test_strategy_c.py \
       tests/unit/test_quote_engine.py \
       tests/unit/test_sports_adapter.py -v
```

---

## Step 7 — Order Diff Actor and Executor

**PRD refs:** §4.5, §5.1.2, FR-201, FR-202, FR-206–210a, FR-216

**Files to create:**
```
core/execution/order_diff.py
core/execution/order_executor.py
tests/unit/test_order_diff.py
tests/unit/test_order_executor.py
```

**`core/execution/order_diff.py`** — Desired/Live/Confirmed state model:

`OrderIntent` is imported from `core.execution.types` (defined in Step 3).
Do not redefine it here.

```python
from core.execution.types import OrderIntent

def compute_mutations(desired: list[OrderIntent],
                      confirmed: list[Order]) -> Mutations:
    """
    Minimum set of (places, cancels).
    Rules applied in order:
    1. FR-210a: before any placement, check for own opposing order within 1 tick.
       If found, add to cancels first.
    2. Post-Only on all Strategy A and C intents.
    3. Price rounding: 2dp standard; 3dp when price < 0.04 or > 0.96 (FR-202).
    """
```

**`core/execution/order_executor.py`:**
```python
def compute_gtd_expiration(target_cutoff_unix: int, buffer_ms: int) -> int:
    """
    buffer_ms is ALWAYS milliseconds (matches settings _MS naming convention).
    Convert with integer division: buffer_ms // 1000
    Returns: target_cutoff_unix - (buffer_ms // 1000) + 60
    (+60 = platform 1-minute security threshold, PRD §5.1.4)
    """

async def execute_cycle(mutations: Mutations, clob_client,
                        settings: Settings, alerts) -> ExecutionResult:
    """
    Step 0 — DRY_RUN guard:
      If settings.DRY_RUN is True, log every place/cancel intent with all
      FR-601 fields and return immediately without calling the CLOB API.
      Simulated fill generation (for paper trading) is added in Step 15 on
      top of this skip — this guard must be present from Step 7 onward so
      that unit and integration tests never hit live endpoints.

    Step 1 — GTD guard (pre-submission):
      For each GTD intent: if expiration < int(time.time()) + 60
      → log error, skip — never submit an already-expired GTD order.

    Step 2 — Dispatch batch cancel as a tracked asyncio task (do NOT await the
      result, but DO store the task reference so failures are observable — use
      asyncio.create_task() and attach a done-callback that logs any exception).
      This preserves the fire-and-forget latency benefit while keeping failures
      visible.

    Step 3 — Immediately dispatch batch place.

    Step 4 — On DUPLICATE_ORDER_ID rejection:
      Retry: 10ms → 25ms → 50ms (3 attempts)
      After 3 failures: call open-orders query (force reconciliation),
      skip this market's cycle, log as sequencing failure.

    Step 5 — Rejection rate tracking:
      Track duplicate-ID rejections as % of placements over rolling 60s.
      If rate > CANCEL_CONFIRM_THRESHOLD_PCT:
        switch to confirm-cancel mode (await cancel before placing).
        Log and send CANCEL_CONFIRM_MODE_ACTIVATED alert on transition.

    Step 6 — FR-606 Builder attribution: attach Builder attribution headers to
      every order submission for Builder Leaderboard credit. These are HTTP
      headers added to each POST request; consult the Builder Program docs for
      the exact header names. Absence of these headers does not affect order
      validity but forfeits Leaderboard attribution.
      HTTP 429 → exponential backoff.
      Response delay > REQUEST_TIMEOUT_S → treat as soft throttle signal;
      back off 250ms; after 3 consecutive timeouts: alert + pause market 1 cycle.
    """
```

**`tests/unit/test_order_diff.py`** must cover:
- `Desired=[BUY@0.60], Confirmed=[]` → place
- `Desired=[], Confirmed=[BUY@0.60]` → cancel
- `Desired=[BUY@0.61], Confirmed=[BUY@0.60]` → cancel old, place new
- FR-210a: `Desired=[BUY@0.60]` + `Confirmed=[SELL@0.61]` (1 tick apart)
  → SELL cancel added to mutations before BUY placement
- Post-Only applied to Strategy A and C; not B
- Price rounding: `0.037` → `0.037` (3dp); `0.50` → `0.50` (2dp);
  `0.964` → `0.964` (3dp)

**`tests/unit/test_order_executor.py`** must cover:
- DRY_RUN=True: `execute_cycle()` logs all intents and returns without calling
  `clob_client` — assert `clob_client` mock receives zero calls
- DRY_RUN=False: `clob_client` is called normally
- GTD guard: `expiration = int(time.time()) - 10` → order skipped, error logged
- GTD guard: future expiration → order proceeds normally
- GTD formula: `target=1_700_000_000, buffer_ms=7_200_000` →
  `result = 1_700_000_000 - 7200 + 60 = 1_699_993_860`; assert `result > 0`
- Fire-and-forget: cancel dispatched before place; no await on cancel
- Retry: DUPLICATE_ORDER_ID → 3 attempts at 10ms/25ms/50ms backoff
- After 3 failures: reconciliation triggered, cycle skipped
- Confirm-cancel mode activates at `CANCEL_CONFIRM_THRESHOLD_PCT`
- Delayed response > `REQUEST_TIMEOUT_S` → 250ms backoff (not hard failure)
- HTTP 429 → exponential backoff

**Acceptance gate:**
```bash
pytest tests/unit/test_order_diff.py tests/unit/test_order_executor.py -v
```

---

## Step 8 — Universe Scanner and Market Ranker

**PRD refs:** §4.4.2, FR-101–104, FR-103a, FR-119, FR-157, FR-401, §8.3, §8.3a

**Files to create:**
```
core/control/universe_scanner.py
core/control/market_ranker.py
core/control/parameter_service.py
tests/unit/test_universe_scanner.py
tests/unit/test_market_ranker.py
```

`core/control/sports_adapter.py` was built in Step 6 (Strategy A requires it
at gate 10). Step 8 imports it but does not recreate it.

**`core/control/universe_scanner.py`:**
- `scan_universe() -> list[dict]` — Gamma API, 50/page, all active events
- `scan_single_market(condition_id) -> dict` — for resolution polling
- Two asyncio tasks:
  1. `catalog_scan_loop()` — every `SCAN_INTERVAL_MS`; for each scanned market,
     builds a new `MarketCapabilityModel` via `enrich()`, then calls
     `detect_mutations(prev_snapshot, new_model)` where `prev_snapshot` is the
     last model stored in the scanner's own `dict[condition_id, MarketCapabilityModel]`.
     Emits `MutationEvent` for each detected mutation type, then updates the
     snapshot dict with the new model. The capability model itself carries no
     prior-state fields — all snapshot state lives in this scanner dict.
  2. `resolution_polling_loop()` — every `REDEMPTION_POLL_INTERVAL_S`; enqueues
     resolved markets for auto-redemption

**`core/control/market_ranker.py`:**
```python
def compute_ev(market, fill_history, reward_history,
               inventory, settings) -> MarketEV:
    """
    maker_EV = spread_EV + reward_EV + rebate_EV
              - adverse_selection_cost - inventory_cost - event_risk_cost

    Cold-start (< 100 fills OR < 24h of data for this market):
      fill_probability = cold_start_prior(distance_ticks)
      EXACT formula — implement precisely:
        max(0.05, 0.5 - (distance_ticks * 0.09))
        distance_ticks=0 → 0.50
        distance_ticks=1 → 0.41
        distance_ticks=3 → 0.23
        distance_ticks=5 → 0.05  (floor)
        distance_ticks=6 → 0.05  (clamped — never below 0.05)
      adverse_selection_cost = 1 tick (fixed conservative penalty)
      ranking falls back to: spread_width, rewardsDailyRate, liquidity_depth

    Steady-state:
      adverse_selection_cost = mean 30s markout from fill history
      fill_probability = historical fill rate at posted distance from mid

    Reward data (FR-157, FR-401):
      Candidate discovery: Gamma markets?rewards=true (secondary only)
      Authoritative config: GET /rewards/markets/current (primary)
      User reward %:        GET /rewards/user/percentages (primary)
      Scoring status:       GET /order-scoring?order_id={id} per order (primary)
    """

def rank_and_allocate(evs: list[MarketEV],
                      settings: Settings) -> list[MarketAllocation]:
    """
    1. Exclude EV <= 0
    2. Sort descending by EV
    3. Take top MM_MAX_MARKETS
    4. Normalise scores to sum 1.0
    5. Each market: max(MM_MIN_ORDER_SIZE, normalised_EV × budget)
    6. Subject to MAX_PER_MARKET and MAX_TOTAL_EXPOSURE
    """
```

**`tests/unit/test_universe_scanner.py`** must cover:
- `scan_universe()` paginates correctly (mock two pages of 50 markets each)
- `catalog_scan_loop()` emits `MutationEvent` for each of the 4 FR-103a types
  when the new scan differs from the stored snapshot
- Snapshot dict updated after each scan cycle
- `resolution_polling_loop()` enqueues resolved markets when `resolved=True`

**`tests/unit/test_market_ranker.py`** must cover:
- Cold-start prior exact values: `prior(0)=0.50`, `prior(1)=0.41`,
  `prior(3)=0.23`, `prior(5)=0.05`, `prior(6)=0.05` (clamped at floor)
- Cold-start: < 100 fills → `adverse_selection_cost = 1 tick`
- EV ≤ 0 markets excluded
- Top N bounded by `MM_MAX_MARKETS`
- Allocation proportional to normalised EV
- Each market gets at least `MM_MIN_ORDER_SIZE` per side

**`core/control/parameter_service.py`** — PRD §4.4.2 Control Plane module:
```python
class ParameterService:
    """
    Stores and distributes configuration. Supports live tuning without code changes.
    Versions every config change for postmortems.
    """
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, reason: str) -> None:
        """Writes to Redis, logs change with timestamp and reason string."""
    def history(self, key: str, n: int = 10) -> list[ConfigChange]: ...
```
At startup, the ParameterService loads from `Settings` as baseline. Live overrides
are applied via `set()` and propagate to any module that reads through this service.
This is the mechanism for `STRATEGY_*_ENABLED`, spread parameters, and latency
thresholds to be adjusted at runtime without restarting the process.

**Acceptance gate:**
```bash
pytest tests/unit/test_universe_scanner.py \
       tests/unit/test_market_ranker.py -v
```

---

## Step 9 — Risk Gate

**PRD refs:** FR-301–310, FR-309

**Files to create:**
```
core/execution/risk_gate.py
tests/unit/test_risk_gate.py
```

**`core/execution/risk_gate.py`:**
```python
class RiskGate:
    async def check(self, intent: OrderIntent,
                    context: RiskContext) -> RiskDecision:
        """
        Synchronous pre-trade hard checks — first failure returns BLOCKED.
        Checks in order:
        1. Total exposure < MAX_TOTAL_EXPOSURE
        2. Per-market exposure < MAX_PER_MARKET
        3. Daily P&L > -MAX_DAILY_LOSS
        4. Drawdown from peak < MAX_DRAWDOWN
        5. Session health (heartbeat live, streams connected)
        6. FR-309 defence-in-depth: verify accepting_orders == True.
           Log as data-layer validation failure if discrepancy found
           (Universe Scanner is the authoritative gate; this is a backstop).
        """

    async def activate_kill_switch(self, clob_client, alerts):
        """
        FR-211: call cancel_all() within 5 seconds.
        Allow in-flight CTF redemptions to complete.
        Queued-but-unsubmitted redemptions: log redemption_interrupted=True, alert.
        """

    async def reset_daily_counters(self):
        """00:00 UTC. Resumes trading if halted ONLY due to daily loss limit."""
```

FR-310: if P95 > `LATENCY_ALERT_P95_MS` for 60 consecutive seconds → alert +
cut active subscription count by 50% until recovered.

**`tests/unit/test_risk_gate.py`** must cover:
- Each of the 6 checks independently blocks when condition fails
- Kill switch completes `cancel_all()` within 5 seconds
- Daily counter reset at UTC midnight → resumes from daily-loss halt only
- FR-309: `accepting_orders=False` at Risk Gate → breach logged, order blocked

**Acceptance gate:**
```bash
pytest tests/unit/test_risk_gate.py -v
```

---

## Step 10 — Ledgers, Recovery, and Auto-Redemption

**PRD refs:** FR-215, FR-503–506, FR-601a

**Files to create:**
```
core/ledger/order_ledger.py
core/ledger/fill_ledger.py
core/ledger/position_ledger.py
core/ledger/reward_ledger.py
core/ledger/recovery.py
tests/unit/test_order_ledger.py
tests/unit/test_fill_ledger.py
tests/unit/test_recovery.py
```

**`core/ledger/order_ledger.py`** — persists every order lifecycle event:
```python
async def record_submission(order: OrderIntent, order_id: str,
                             postgres_client) -> None:
    """Writes status=SUBMITTED on placement."""

async def record_ack(order_id: str, postgres_client) -> None:
    """Updates status=CONFIRMED on User channel acknowledgement."""

async def record_cancel(order_id: str, postgres_client) -> None:
    """Updates status=CANCELLED on cancel acknowledgement."""

async def record_rejection(order_id: str, reason: str,
                            postgres_client) -> None:
    """Updates status=REJECTED on placement failure."""
```
All writes use the `time_in_force` + `post_only` schema columns (not a single
`order_type` column — see Step 2 schema split).

**`core/ledger/fill_ledger.py`:**
```python
async def record_fill(fill: FillEvent, postgres_client) -> None:
    """
    Persists all fields to fills table.
    maker_taker: from User channel event — NOT inferred from order type (FR-453).
    simulated: False for live fills; True for DRY_RUN fills.
    """

async def schedule_markout(fill_id: int, token_id: str,
                            fill_ts: datetime, book: BookStateStore) -> None:
    """
    FR-601a: schedules 30s asyncio task.
    Records mid_at_fill immediately.
    30s later: records mid_at_30s; computes markout_30s.
    markout_30s = (mid_at_30s - mid_at_fill) × side_sign
    Positive = adverse; negative = favourable.
    """
```

**`core/ledger/recovery.py`:**
```python
async def rebuild_confirmed_state(clob_client, data_api) -> ConfirmedState:
    """
    FR-502, FR-504: called on every startup and reconnect.
    Queries open-orders endpoint + Data API positions.
    Quoting MUST NOT resume until this returns successfully.
    """

async def auto_redeem(market: ResolvedMarket, relayer_client,
                       postgres_client, settings, alerts) -> None:
    """
    FR-215 — CTF contract: redeemPositions() with 4-argument signature:
      redeemPositions(
        collateralToken,       # USDC.e address on Polygon
        parentCollectionId,    # bytes32(0)
        conditionId,           # market.condition_id
        indexSets              # [1 << winning_outcome_index]  (bitmask)
      )
    Submit via Builder Relayer (gasless).
    Retry: 3× with backoff 30s → 120s → 300s.
    On 3 failures: log + REDEMPTION_FAILED_MANUAL_REQUIRED alert.
    On success:
      - Write market_id to ledger immediately (FR-506, prevents double-redemption)
      - Send REDEMPTION_SUCCESS alert
    Second call for same market_id is a no-op (check ledger first).
    """
```

**`tests/unit/test_order_ledger.py`** must cover:
- `record_submission` writes all fields with `status=SUBMITTED`
- `record_ack` transitions status to `CONFIRMED`
- `record_cancel` transitions status to `CANCELLED`
- `record_rejection` transitions status to `REJECTED` with reason stored
- Schema uses `time_in_force` and `post_only` columns; no `order_type` column

**`tests/unit/test_fill_ledger.py`** must cover:
- All fields persisted including `maker_taker` and `simulated`
- Markout task fires 30s after fill; `markout_30s` computed with correct sign
- Positive markout = adverse; negative = favourable

**`tests/unit/test_recovery.py`** must cover:
- `rebuild_confirmed_state` merges open-orders query + Data API
- `auto_redeem` calls contract with correct 4-arg signature
- `indexSets = [1 << winning_outcome_index]` bitmask verified
- Retry sequence: 30s/120s/300s backoff; alert on final failure
- Redeemed `market_id` written to ledger on success
- Second `auto_redeem` call for same market_id is no-op

**Acceptance gate:**
```bash
pytest tests/unit/test_order_ledger.py \
       tests/unit/test_fill_ledger.py \
       tests/unit/test_recovery.py -v
```

---

## Step 11 — Observability

**PRD refs:** FR-601–606

**Files to create:**
```
alerts/dispatcher.py
metrics/prometheus.py
tests/unit/test_alerts.py
tests/unit/test_metrics.py
```

**`alerts/dispatcher.py`** — async Telegram + Discord dispatcher:
```python
class AlertEvent(Enum):
    KILL_SWITCH_ACTIVATED = auto()
    DAILY_LOSS_LIMIT_HIT = auto()
    WS_DISCONNECT_60S = auto()
    INVENTORY_HALT_TRIGGERED = auto()
    ZERO_TRADES_30MIN = auto()
    MARKET_RESOLVED_WITH_POSITIONS = auto()
    LATENCY_P95_EXCEEDED = auto()
    RELAYER_FAILOVER_ACTIVATED = auto()
    RELAYER_RECOVERED = auto()
    FEE_CACHE_SUSTAINED_OUTAGE = auto()
    REDEMPTION_FAILED_MANUAL_REQUIRED = auto()
    REDEMPTION_SUCCESS = auto()
    SAFE_MODE_ENTERED = auto()
    SAFE_MODE_EXITED = auto()
    CANCEL_CONFIRM_MODE_ACTIVATED = auto()
```

**`metrics/prometheus.py`** — FR-605 metrics, exact types and buckets:
```python
bot_pnl_daily      = Gauge('bot_pnl_daily', ...)        # resets at UTC midnight
bot_trades_total   = Counter('bot_trades_total', ...)    # cumulative
bot_latency_p95_ms = Histogram('bot_latency_p95_ms', ...,
                        buckets=[10, 25, 50, 75, 100, 150, 200])
# Note: the metric name says 'p95' but the Histogram stores raw latency samples.
# The actual P95 value is derived in PromQL:
#   histogram_quantile(0.95, rate(bot_latency_p95_ms_bucket[5m]))
# The name matches the PRD label and alert threshold; the semantics are correct.
bot_maker_ratio    = Gauge('bot_maker_ratio', ...)       # rolling 1h, A+C only
bot_exposure_total = Gauge('bot_exposure_total', ...)    # USD
bot_drawdown       = Gauge('bot_drawdown', ...)          # USD from peak
```

**`tests/unit/test_alerts.py`** — mock HTTP; verify every `AlertEvent` produces
a non-empty dispatch to at least one channel.

**`tests/unit/test_metrics.py`** — verify all 6 metrics have correct type and
`bot_latency_p95_ms` has exactly the 7 specified buckets.

Step 11 also builds the two periodic reporting loops and the stale-quote safety net.
Add these to `core/execution/` and `alerts/`:

**`core/execution/reporting.py`** — FR-602 and FR-604:
```python
async def status_report_loop(metrics, ledgers, settings, alerts):
    """
    FR-602: emit structured status report every 30 seconds containing:
    total_exposure, daily_pnl, drawdown, trade_count,
    active_subscriptions, inventory_warnings, p95_latency_ms.
    Logs as structured JSON and updates Prometheus gauges.
    """

async def daily_summary_loop(metrics, ledgers, settings, alerts):
    """
    FR-604: at 00:00 UTC emit daily summary containing:
    total_trades, net_pnl, top_markets_by_pnl, exposure_by_strategy,
    estimated_liquidity_rewards, estimated_maker_rebates,
    maker_taker_ratio (A+C only), avg_latency_ms,
    order_scoring_success_rate.
    Also resets bot_pnl_daily Gauge to 0.
    """

async def stale_quote_loop(active_orders, order_executor, settings):
    """
    FR-212: safety-net loop. Every STALE_QUOTE_TIMEOUT_S seconds, scan all
    active orders. Any order not refreshed within STALE_QUOTE_TIMEOUT_S
    is cancelled immediately. This is a backstop — in normal operation the
    event-driven cancel/replace should keep quotes current. This loop catches
    any quotes that fall through due to WS gaps or executor failures.
    """
```

**Acceptance gate:**
```bash
pytest tests/unit/test_alerts.py tests/unit/test_metrics.py -v
```

---

## Step 12 — Orchestrator

**PRD refs:** §4.6, §4.4, FR-211

**Files to create:**
```
core/orchestrator.py
bot/__main__.py
scripts/migrate.py
tests/integration/test_pipeline.py
tests/integration/test_reconnect.py
```

**`bot/__main__.py`** — process entry point:
```python
if __name__ == "__main__":
    import uvloop
    asyncio.run(Orchestrator().start(), loop_factory=uvloop.new_event_loop)
```

**`scripts/migrate.py`** — standalone migration runner:
```bash
python scripts/migrate.py  # applies any unapplied migrations and exits
```

**`core/orchestrator.py`** — `async start()` — startup order is critical:
```
1.  Load settings
2.  Derive CLOB credentials (auth/credentials.py)
3.  Get or deploy Gnosis Safe (auth/relayer.py)
4.  FR-111 RPC latency check: ping Polygon RPC and measure RTT.
    If RTT > RPC_MAX_LATENCY_MS → abort startup with clear error message.
5.  Verify USDC.e balance ≥ MIN_USDC_BALANCE; abort if below
6.  Connect to Postgres; run_migrations()
7.  Connect to Redis
8.  FR-502/FR-504: call rebuild_confirmed_state() to reconstruct live
    operational state from open-orders query + Data API positions.
    Quoting must not begin until this completes successfully.
9.  Construct + CONNECT Market Stream Gateway (WSS handshake must complete)
10. Construct + CONNECT User Stream Gateway (WSS handshake must complete)
11. Start liveness loops — ONLY after 9+10 confirmed connected:
      order_safety_heartbeat_loop
      market_user_ws_heartbeat_loop
      sports_ws_heartbeat_loop (if applicable)
12. Start Universe Scanner catalog loop
13. Start Universe Scanner resolution polling loop
14. Start reporting loops (status_report_loop, daily_summary_loop)
15. Start stale_quote_loop (FR-212)
16. Start main event loop
```

Main event loop routing:
```
BookEvent      → BookStateStore.update()
                 → QuoteEngine.evaluate()
                 → OrderDiff.compute_mutations()
                 → RiskGate.check()
                 → OrderExecutor.execute_cycle()

FillEvent      → FillLedger.record_fill()
                 → FillLedger.schedule_markout()
                 → InventoryState.apply_fill()
                 → FeeCache.on_fill()
                 → QuoteEngine.re_evaluate()

MutationEvent  → RESOLUTION_TIME_CHANGED:
                   recompute GTD expiries, cancel/reprice
                 → ACCEPTING_ORDERS_FLIPPED_FALSE:
                   cancel all quotes for that market
                 → FEE_RATE_CHANGED:
                   FeeCache.invalidate(), re-evaluate
                 → SECONDS_DELAY_BECAME_NONZERO:
                   cancel all quotes, exclude market

ResolvedMarket → Recovery.auto_redeem()
```

`async stop()`:
```
1. Activate kill switch (cancel_all())
2. Allow in-flight CTF redemptions to complete (FR-211)
3. Flush Redis state to Postgres
4. Close WebSocket connections
5. Close DB connections
```

**`tests/integration/test_pipeline.py`** (mock CLOB):
- Fake WS book event → order placement reaches mock CLOB
- Fill event → markout scheduled, inventory updated, fee re-fetched
- `RESOLUTION_TIME_CHANGED` mutation → GTD expiries recomputed and orders repriced

**`tests/integration/test_reconnect.py`:**
- WS drop → reconnect fires with backoff
- After reconnect → `rebuild_confirmed_state()` called before any placement
- No placements occur during reconciliation window

**Acceptance gate:**
```bash
pytest tests/integration/test_pipeline.py \
       tests/integration/test_reconnect.py -v
```

---

## Step 13 — Remaining Integration Tests

**PRD refs:** §11.2

**Files to create:**
```
tests/integration/test_fee_cache_outage.py
tests/integration/test_resync.py
tests/integration/test_redemption.py
tests/integration/test_relayer_failover.py
tests/integration/test_inventory_cycle.py
tests/integration/test_negrisk.py
tests/integration/test_self_cross.py
```

**`test_fee_cache_outage.py`:**
- 5 consecutive misses → market excluded, alert sent
- Cache warms → market re-enters on next scan cycle

**`test_resync.py`** — five cases:
1. Stable + periodic: advance time by `BOOK_RESYNC_INTERVAL_S` → REST resync,
   no cancels, immediate quote recompute
2. Delta threshold: `BOOK_RESYNC_DELTA_THRESHOLD` missed deltas → resync
3. Escalation — mid move > `BOOK_RESYNC_CANCEL_MID_PCT` → quotes cancelled
4. Escalation — spread > `BOOK_RESYNC_CANCEL_SPREAD_TICKS` → quotes cancelled
5. Escalation — gap > `BOOK_RESYNC_CANCEL_GAP_MS` → quotes cancelled

**`test_redemption.py`:**
- Resolution → `redeemPositions()` with correct 4-arg signature
- Within 1h of resolution
- 3 retries at 30s/120s/300s; alert on final failure
- Same market_id second call → no-op

**`test_relayer_failover.py`:**
- Relayer down for `EOA_FALLBACK_TIMEOUT_S` → EOA activated, alert
- Recovery → reverts to Relayer, RELAYER_RECOVERED alert

**`test_inventory_cycle.py`:**
- Fills drive value-weighted skew to halt threshold → halt + alert
- Skew recovers below resume threshold → quoting resumes

**`test_negrisk.py`:**
- Strategy A: negRisk market → `negRisk=True` in EIP-712 payload
- Strategy C: negRisk market always excluded regardless of probability

**`test_self_cross.py`:**
- Own resting SELL@0.61, new BUY signal@0.60 (1 tick apart)
- SELL cancel added to mutations before BUY placement (FR-210a)

**Acceptance gate:**
```bash
pytest tests/integration/ -v \
  --cov=core --cov=strategies --cov=fees --cov=inventory \
  --cov-report=term-missing
```
Coverage target: > 90% on Strategy Engine, Fee Calculator, Capability Enricher,
Order Diff Actor, Risk Gate.

---

## Step 14 — Validation Scripts

**PRD refs:** §11.3, §11.4

**Files to create:**
```
scripts/approve_contracts.py
scripts/smoke_test.py
scripts/shadow_run.py
scripts/paper_trading_report.py
```

**`scripts/approve_contracts.py`** — one-time setup:
- Approve USDC.e for Polymarket CLOB Exchange contract
- Approve CTF contract
- Uses `web3==6.14.0`; idempotent (safe to re-run)

**`scripts/smoke_test.py`** — §11.3:
- Place $0.01 Post-Only GTC limit order with correct `feeRateBps` on a live
  low-volume market
- Immediately cancel it
- Verify cancel acknowledgement received
- Exit 0 on PASS, exit 1 on FAIL
- **Do not proceed to shadow_run.py if this exits 1**

**`scripts/shadow_run.py`** — §11.4 Step 1 (24-hour latency gate):
- `DRY_RUN=false`, `MAX_TOTAL_EXPOSURE=0`
- Risk Gate cancels every order immediately after dispatch
- Collects real P95 cancel/replace latency
- Exit 0 if P95 < 100ms; exit 1 if ≥ 100ms
- `DRY_RUN=true` cannot substitute — it skips API calls entirely

**`scripts/paper_trading_report.py`** — reads JSON logs; computes 8 criteria
from §11.4 Step 2:

> ⚠️ Criteria 1–2 use simulated fills. They confirm the state machine runs.
> They do NOT validate Strategy A profitability or adverse selection risk.
> The live markout gate (§11.4 Step 3) is the only real readiness signal.

1. Simulated P&L positive for ≥ 10 of 14 days `[SIMULATED — NOT PROFITABILITY]`
2. Simulated max drawdown ≤ 5% of capital `[SIMULATED — NOT PROFITABILITY]`
3. Trade count average > 100/day
4. Fee cache hit ratio > 95%
5. Zero inventory halt events in first 7 days
6. Resolution watchlist correctly flags > 95% of resolved markets
7. Auto-redemption simulation: ≥ 3 markets simulated
8. Order scoring trackable for ≥ 5 reward-eligible markets

**Acceptance gate:** All scripts import without error. `smoke_test.py` exits 0
against live API (requires funded wallet and real `.env`).

---

## Step 15 — DRY_RUN Mode and Paper Trading

**PRD refs:** §11.4 Step 2

**Files to modify:**
```
core/ledger/fill_ledger.py
```

> ⚠️ SCOPE LIMIT: The fill simulator is scaffolding for state-machine validation
> only. Fill probability (0.3) is intentionally arbitrary. Do not build a
> sophisticated simulator. Do not use simulated P&L to decide whether to deploy
> capital. The live markout gate is the only valid Strategy A readiness signal.

The DRY_RUN API-skip (log intent, do not call CLOB) was added to
`order_executor.py` in Step 7. This step adds only the **fill simulation**
layer on top — no changes to the executor are required here.

**`fill_ledger.py` simulated fill generation:**
When `DRY_RUN=True`: generate a simulated fill with fixed probability 0.3 at
the quoted price. Tag all simulated fills `simulated=True`. Never mix simulated
fills with live fills in markout calculations.

**Acceptance gate:** Run `DRY_RUN=true` for 48 hours. `paper_trading_report.py`
produces output for all 8 criteria without errors. Criteria 3–8 passing is
meaningful; criteria 1–2 confirm the state machine runs.

---

## Summary Table

| Step | Builds | Acceptance Gate |
|------|--------|-----------------|
| 0 | Scaffold, settings | Settings unit tests |
| 1 | Auth, credentials, Relayer | Auth unit tests |
| 2 | Storage, migrations | Storage unit tests |
| 3 | WS streams, liveness loops, shared types (`types.py`) | Book state + liveness unit tests |
| 4 | Capability enricher, fee engine | Enricher + fee unit tests |
| 5 | Inventory manager | Value-weighted skew unit tests |
| 6 | All three strategies, sports adapter | Strategy + sports adapter unit tests |
| 7 | Order diff, executor (incl. DRY_RUN API-skip) | Diff + executor unit tests |
| 8 | Universe scanner, market ranker | Ranker unit tests |
| 9 | Risk gate | Risk gate unit tests |
| 10 | Ledgers, auto-redemption | Fill ledger + recovery unit tests |
| 11 | Alerts, Prometheus | Alert + metrics unit tests |
| 12 | Orchestrator, wiring | Pipeline + reconnect integration tests |
| 13 | Remaining integration tests | Full suite > 90% coverage |
| 14 | Validation scripts | Smoke test passes against live API |
| 15 | DRY_RUN fill simulation (paper trading) | 48h dry run, report generates cleanly |

---

## Post-Build Deployment Sequence

After Step 15 passes, proceed in strict order:

**1. Shadow run** (`scripts/shadow_run.py`, 24 hours)
P95 latency must be < 100ms before proceeding. If ≥ 100ms, investigate
infrastructure — not code.

**2. 14-day paper trading** (`DRY_RUN=true`)
Run `scripts/paper_trading_report.py` daily. All 8 criteria must pass.

**3. Live ramp** — deploy with §11.5 ramp config:
```
STRATEGY_A_ENABLED=true
STRATEGY_B_ENABLED=false
STRATEGY_C_ENABLED=false
STRATEGY_A_UNIVERSE_TAGS=["crypto"]   # or ["sports","esports"] — pick one
MM_ORDER_SIZE=5
MAX_TOTAL_EXPOSURE=400
```

**4. Markout gate** (§11.4 Step 3) — 7 live days:
- Median 30s markout ≤ +0.5¢
- < 30% of fills with markout > +1¢
- Net markout P&L positive

**5. Scale** — only after markout gate passes:
- Raise `MAX_TOTAL_EXPOSURE` to $2,000
- Enable `STRATEGY_C_ENABLED=true`
- Enable `STRATEGY_B_ENABLED=true` (optional; may defer to v1.1)
