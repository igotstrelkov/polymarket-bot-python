import json
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Operational ───────────────────────────────────────────────────────────
    DRY_RUN: bool = True
    USE_RELAYER: bool = True
    POLYGON_RPC_URL: str
    PRIVATE_KEY: str
    BUILDER_API_KEY: str
    BUILDER_SECRET: str
    BUILDER_PASSPHRASE: str
    REDIS_URL: str = "redis://localhost:6379"
    DATABASE_URL: str = ""
    STATE_FILE_PATH: str = "./state.json.enc"
    STATE_ENCRYPTION_KEY: str = ""

    # ── Strategy enablement ───────────────────────────────────────────────────
    STRATEGY_A_ENABLED: bool = True
    STRATEGY_B_ENABLED: bool = True
    STRATEGY_C_ENABLED: bool = True
    STRATEGY_A_UNIVERSE_TAGS: list[str] = []

    # ── Heartbeat ─────────────────────────────────────────────────────────────
    HEARTBEAT_INTERVAL_MS: int = 5000

    # ── Fee engine ────────────────────────────────────────────────────────────
    FEE_CACHE_TTL_S: int = 30
    FEE_CONSECUTIVE_MISS_THRESHOLD: int = 5
    FEE_DEVIATION_THRESHOLD_PCT: float = 10.0

    # ── Book resync ───────────────────────────────────────────────────────────
    BOOK_RESYNC_INTERVAL_S: int = 60
    BOOK_RESYNC_DELTA_THRESHOLD: int = 5
    BOOK_RESYNC_CANCEL_MID_PCT: float = 0.5
    BOOK_RESYNC_CANCEL_SPREAD_TICKS: int = 10
    BOOK_RESYNC_CANCEL_GAP_MS: int = 2000

    # ── Order execution ───────────────────────────────────────────────────────
    CANCEL_CONFIRM_THRESHOLD_PCT: float = 5.0
    REQUEST_TIMEOUT_S: int = 10

    # ── Market making ─────────────────────────────────────────────────────────
    MM_BASE_SPREAD: float = 0.04
    MM_COST_FLOOR: float = 0.01
    MM_ORDER_SIZE: int = 10
    MM_MIN_ORDER_SIZE: int = 0
    MM_MAX_MARKETS: int = 20

    # ── GTD buffers ───────────────────────────────────────────────────────────
    GTD_RESOLUTION_BUFFER_MS: int = 7_200_000
    GTD_GAME_START_BUFFER_MS: int = 300_000

    # ── Strategy B ────────────────────────────────────────────────────────────
    PENNY_MIN_PRICE: float = 0.001
    PENNY_MAX_PRICE: float = 0.03
    PENNY_BUDGET: float = 5.0
    PENNY_MAX_TOTAL: float = 200.0

    # ── Strategy C ────────────────────────────────────────────────────────────
    SNIPE_PROB_THRESHOLD: float = 0.90
    SNIPE_MAX_FEE_BPS: int = 5
    SNIPE_MIN_SIZE: int = 5
    SNIPE_MAX_SIZE: int = 20
    SNIPE_MAX_POSITION: float = 50.0

    # ── Inventory ─────────────────────────────────────────────────────────────
    INVENTORY_SKEW_THRESHOLD: float = 0.65
    INVENTORY_HALT_THRESHOLD: float = 0.80
    INVENTORY_RESUME_THRESHOLD: float = 0.70
    INVENTORY_SKEW_MULTIPLIER: int = 3

    # ── Risk ──────────────────────────────────────────────────────────────────
    MAX_TOTAL_EXPOSURE: float = 2000.0
    MAX_PER_MARKET: float = 100.0
    MAX_DAILY_LOSS: float = 500.0
    MAX_DRAWDOWN: float = 500.0

    # ── Watchlist ─────────────────────────────────────────────────────────────
    RESOLUTION_WARN_MS: int = 7_200_000
    RESOLUTION_PULL_MS: int = 1_800_000
    STALE_QUOTE_TIMEOUT_S: int = 60

    # ── Infrastructure ────────────────────────────────────────────────────────
    SCAN_INTERVAL_MS: int = 300_000
    REDEMPTION_POLL_INTERVAL_S: int = 60
    EOA_FALLBACK_TIMEOUT_S: int = 30
    MIN_USDC_BALANCE: float = 100.0
    RPC_MAX_LATENCY_MS: int = 100
    LATENCY_ALERT_P95_MS: int = 150
    REDIS_OUTAGE_HALT_S: int = 300
    POSTGRES_BUFFER_MAX_ROWS: int = 10_000
    PROMETHEUS_PORT: int = 9090
    TELEGRAM_WEBHOOK_URL: str = ""
    DISCORD_WEBHOOK_URL: str = ""

    @field_validator("STRATEGY_A_UNIVERSE_TAGS", mode="before")
    @classmethod
    def parse_tags(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return json.loads(v)
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
