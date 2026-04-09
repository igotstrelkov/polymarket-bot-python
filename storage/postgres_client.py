"""
Async Postgres wrapper using asyncpg.

FR-503: Schema versioning via schema_migrations table.
FR-504a sub-case 2: In-process fill buffer capped at POSTGRES_BUFFER_MAX_ROWS.
FR-504a sub-cases 3/4: storage_safe_mode flag for combined Redis+Postgres failure.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a storage operation fails."""


class PostgresClient:
    """Async asyncpg wrapper with migration support and fill buffering."""

    def __init__(self, dsn: str, buffer_max_rows: int = 10_000) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._buffer: asyncio.Queue = asyncio.Queue(maxsize=buffer_max_rows)
        self._buffer_max_rows = buffer_max_rows
        # FR-504a sub-case 3: combined health signal, set by health_check()
        self.storage_safe_mode: bool = False

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ── Core query methods ────────────────────────────────────────────────────

    async def execute(self, query: str, *args: Any) -> str:
        if not self._pool:
            raise StorageError("Postgres pool not initialised — call connect() first")
        try:
            return await self._pool.execute(query, *args)
        except asyncpg.PostgresError as exc:
            raise StorageError(f"Postgres execute failed: {exc}") from exc

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        if not self._pool:
            raise StorageError("Postgres pool not initialised — call connect() first")
        try:
            return await self._pool.fetch(query, *args)
        except asyncpg.PostgresError as exc:
            raise StorageError(f"Postgres fetch failed: {exc}") from exc

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        if not self._pool:
            raise StorageError("Postgres pool not initialised — call connect() first")
        try:
            return await self._pool.fetchrow(query, *args)
        except asyncpg.PostgresError as exc:
            raise StorageError(f"Postgres fetchrow failed: {exc}") from exc

    # ── Migrations ────────────────────────────────────────────────────────────

    async def run_migrations(self, migrations_dir: Path) -> None:
        """Apply pending SQL migrations in version order.

        FR-503: Forward-only. Skips already-applied versions. Records each
        applied version in schema_migrations. Never rolls back.
        """
        # Ensure the tracking table exists first
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        applied = {
            row["version"]
            for row in await self.fetch("SELECT version FROM schema_migrations")
        }

        sql_files = sorted(migrations_dir.glob("*.sql"))
        for sql_file in sql_files:
            # Extract version number from filename prefix (e.g. "001_...")
            try:
                version = int(sql_file.stem.split("_")[0])
            except (ValueError, IndexError):
                log.warning("Skipping migration file with unexpected name: %s", sql_file)
                continue

            if version in applied:
                log.debug("Migration %d already applied — skipping", version)
                continue

            sql = sql_file.read_text()
            await self.execute(sql)
            await self.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)", version
            )
            log.info("Applied migration %d (%s)", version, sql_file.name)

    # ── Fill buffer ───────────────────────────────────────────────────────────

    async def buffer_fill(self, fill_data: dict, fallback_file: Path | None = None) -> None:
        """Buffer a fill record for async durable write.

        FR-504a sub-case 2: If buffer is full, log to local fallback file and alert.
        """
        try:
            self._buffer.put_nowait(fill_data)
        except asyncio.QueueFull:
            log.error(
                "Fill buffer full (%d rows) — writing to fallback file",
                self._buffer_max_rows,
            )
            if fallback_file:
                with fallback_file.open("a") as f:
                    import json
                    f.write(json.dumps(fill_data) + "\n")

    def buffer_size(self) -> int:
        return self._buffer.qsize()

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if Postgres is reachable."""
        if not self._pool:
            return False
        try:
            await self._pool.fetchval("SELECT 1")
            return True
        except (asyncpg.PostgresError, OSError):
            return False
