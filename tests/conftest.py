import time

import pytest
from unittest.mock import AsyncMock

from config.settings import Settings
from core.execution.types import BookEvent, PriceLevel


@pytest.fixture
def settings() -> Settings:
    """Settings instance with DRY_RUN=True and test-safe defaults."""
    return Settings(
        DRY_RUN=True,
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://polygon-rpc.example.com",
        BUILDER_API_KEY="test-api-key",
        BUILDER_SECRET="test-secret",
        BUILDER_PASSPHRASE="test-passphrase",
    )


@pytest.fixture
def mock_clob_client() -> AsyncMock:
    """AsyncMock of ClobClient for use in unit tests."""
    return AsyncMock()


def mock_ws_message(
    token_id: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> BookEvent:
    """Factory returning a BookEvent for use in tests."""
    return BookEvent(
        token_id=token_id,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
        timestamp=time.time(),
    )
