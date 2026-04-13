import pytest
from pydantic import ValidationError

from config.settings import Settings


def make_settings(**overrides) -> Settings:
    """Build a valid Settings instance, merging in any overrides.

    _env_file=None prevents pydantic-settings from reading a local .env
    file so tests see only the values explicitly passed here.
    """
    defaults = dict(
        _env_file=None,
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://polygon-rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="passphrase",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Required-field validation ──────────────────────────────────────────────────

def test_private_key_required():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            POLYGON_RPC_URL="https://polygon-rpc.example.com",
            BUILDER_API_KEY="key",
            BUILDER_SECRET="secret",
            BUILDER_PASSPHRASE="passphrase",
        )


def test_polygon_rpc_url_required():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            PRIVATE_KEY="0x" + "a" * 64,
            BUILDER_API_KEY="key",
            BUILDER_SECRET="secret",
            BUILDER_PASSPHRASE="passphrase",
        )


def test_builder_api_key_required():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            PRIVATE_KEY="0x" + "a" * 64,
            POLYGON_RPC_URL="https://polygon-rpc.example.com",
            BUILDER_SECRET="secret",
            BUILDER_PASSPHRASE="passphrase",
        )


def test_builder_secret_required():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            PRIVATE_KEY="0x" + "a" * 64,
            POLYGON_RPC_URL="https://polygon-rpc.example.com",
            BUILDER_API_KEY="key",
            BUILDER_PASSPHRASE="passphrase",
        )


def test_builder_passphrase_required():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            PRIVATE_KEY="0x" + "a" * 64,
            POLYGON_RPC_URL="https://polygon-rpc.example.com",
            BUILDER_API_KEY="key",
            BUILDER_SECRET="secret",
        )


# ── Default values ─────────────────────────────────────────────────────────────

def test_dry_run_default():
    s = make_settings()
    assert s.DRY_RUN is True


def test_use_relayer_default():
    s = make_settings()
    assert s.USE_RELAYER is True


def test_redis_url_default():
    s = make_settings()
    assert s.REDIS_URL == "redis://localhost:6379"


def test_database_url_default():
    s = make_settings()
    assert s.DATABASE_URL == ""


def test_state_file_path_default():
    s = make_settings()
    assert s.STATE_FILE_PATH == "./state.json.enc"


def test_state_encryption_key_default():
    s = make_settings()
    assert s.STATE_ENCRYPTION_KEY == ""


def test_strategy_flags_default():
    s = make_settings()
    assert s.STRATEGY_A_ENABLED is True
    assert s.STRATEGY_B_ENABLED is True
    assert s.STRATEGY_C_ENABLED is True


def test_strategy_a_universe_tags_default():
    s = make_settings()
    assert s.STRATEGY_A_UNIVERSE_TAGS == []


def test_heartbeat_interval_default():
    s = make_settings()
    assert s.HEARTBEAT_INTERVAL_MS == 5000  # NOT 30000


def test_fee_cache_ttl_default():
    s = make_settings()
    assert s.FEE_CACHE_TTL_S == 30


def test_fee_consecutive_miss_threshold_default():
    s = make_settings()
    assert s.FEE_CONSECUTIVE_MISS_THRESHOLD == 5


def test_fee_deviation_threshold_default():
    s = make_settings()
    assert s.FEE_DEVIATION_THRESHOLD_PCT == 10.0


def test_book_resync_interval_default():
    s = make_settings()
    assert s.BOOK_RESYNC_INTERVAL_S == 60  # NOT 10


def test_book_resync_delta_threshold_default():
    s = make_settings()
    assert s.BOOK_RESYNC_DELTA_THRESHOLD == 5


def test_book_resync_cancel_mid_pct_default():
    s = make_settings()
    assert s.BOOK_RESYNC_CANCEL_MID_PCT == 0.5


def test_book_resync_cancel_spread_ticks_default():
    s = make_settings()
    assert s.BOOK_RESYNC_CANCEL_SPREAD_TICKS == 10


def test_book_resync_cancel_gap_ms_default():
    s = make_settings()
    assert s.BOOK_RESYNC_CANCEL_GAP_MS == 2000


def test_cancel_confirm_threshold_default():
    s = make_settings()
    assert s.CANCEL_CONFIRM_THRESHOLD_PCT == 5.0


def test_request_timeout_default():
    s = make_settings()
    assert s.REQUEST_TIMEOUT_S == 10


def test_mm_defaults():
    s = make_settings()
    assert s.MM_BASE_SPREAD == 0.04
    assert s.MM_COST_FLOOR == 0.01
    assert s.MM_ORDER_SIZE == 10
    assert s.MM_MIN_ORDER_SIZE == 0
    assert s.MM_MAX_MARKETS == 20


def test_gtd_buffer_defaults():
    s = make_settings()
    assert s.GTD_RESOLUTION_BUFFER_MS == 7_200_000
    assert s.GTD_GAME_START_BUFFER_MS == 300_000


def test_strategy_b_defaults():
    s = make_settings()
    assert s.PENNY_MIN_PRICE == 0.001
    assert s.PENNY_MAX_PRICE == 0.03
    assert s.PENNY_BUDGET == 5.0
    assert s.PENNY_MAX_TOTAL == 200.0


def test_strategy_c_defaults():
    s = make_settings()
    assert s.SNIPE_PROB_THRESHOLD == 0.90
    assert s.SNIPE_MAX_FEE_BPS == 5
    assert s.SNIPE_MIN_SIZE == 5
    assert s.SNIPE_MAX_SIZE == 20
    assert s.SNIPE_MAX_POSITION == 50.0


def test_inventory_defaults():
    s = make_settings()
    assert s.INVENTORY_SKEW_THRESHOLD == 0.65
    assert s.INVENTORY_HALT_THRESHOLD == 0.80
    assert s.INVENTORY_RESUME_THRESHOLD == 0.70
    assert s.INVENTORY_SKEW_MULTIPLIER == 3


def test_risk_defaults():
    s = make_settings()
    assert s.MAX_TOTAL_EXPOSURE == 2000.0
    assert s.MAX_PER_MARKET == 100.0
    assert s.MAX_DAILY_LOSS == 500.0
    assert s.MAX_DRAWDOWN == 500.0


def test_watchlist_defaults():
    s = make_settings()
    assert s.RESOLUTION_WARN_MS == 7_200_000
    assert s.RESOLUTION_PULL_MS == 1_800_000
    assert s.STALE_QUOTE_TIMEOUT_S == 60


def test_infrastructure_defaults():
    s = make_settings()
    assert s.SCAN_INTERVAL_MS == 300_000
    assert s.REDEMPTION_POLL_INTERVAL_S == 60
    assert s.EOA_FALLBACK_TIMEOUT_S == 30
    assert s.MIN_USDC_BALANCE == 100.0
    assert s.RPC_MAX_LATENCY_MS == 100
    assert s.LATENCY_ALERT_P95_MS == 150
    assert s.REDIS_OUTAGE_HALT_S == 300
    assert s.POSTGRES_BUFFER_MAX_ROWS == 10_000
    assert s.PROMETHEUS_PORT == 9090
    assert s.TELEGRAM_WEBHOOK_URL == ""
    assert s.DISCORD_WEBHOOK_URL == ""


# ── STRATEGY_A_UNIVERSE_TAGS JSON parsing ─────────────────────────────────────

def test_universe_tags_parses_json_string():
    s = make_settings(STRATEGY_A_UNIVERSE_TAGS='["crypto"]')
    assert s.STRATEGY_A_UNIVERSE_TAGS == ["crypto"]


def test_universe_tags_parses_multi_tag_json():
    s = make_settings(STRATEGY_A_UNIVERSE_TAGS='["sports", "esports"]')
    assert s.STRATEGY_A_UNIVERSE_TAGS == ["sports", "esports"]


def test_universe_tags_accepts_list_directly():
    s = make_settings(STRATEGY_A_UNIVERSE_TAGS=["crypto"])
    assert s.STRATEGY_A_UNIVERSE_TAGS == ["crypto"]


def test_universe_tags_empty_json_string():
    s = make_settings(STRATEGY_A_UNIVERSE_TAGS="[]")
    assert s.STRATEGY_A_UNIVERSE_TAGS == []
