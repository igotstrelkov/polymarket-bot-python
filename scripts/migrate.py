"""
Standalone migration runner.

Usage:
    python scripts/migrate.py

Applies any unapplied SQL migrations from storage/migrations/ in version order
and exits. Idempotent — skips already-applied versions.

Requires POSTGRES_DSN to be set in the environment (or .env file).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent.parent / "storage" / "migrations"


async def _run() -> None:
    # Import here so the module can be imported without triggering pydantic validation
    from config.settings import Settings
    from storage.postgres_client import PostgresClient

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    client = PostgresClient(dsn=s.DATABASE_URL)
    try:
        await client.connect()
        log.info("Connected to Postgres")
        await client.run_migrations(_MIGRATIONS_DIR)
        log.info("Migrations complete")
    except Exception as exc:
        log.error("Migration failed: %s", exc)
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(_run())
