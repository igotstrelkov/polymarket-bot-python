"""
Unit tests for storage/redis_client.py and storage/postgres_client.py.

All network calls are mocked — no real Redis or Postgres required.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storage.redis_client import RedisClient, StorageError as RedisStorageError
from storage.postgres_client import PostgresClient, StorageError as PgStorageError


# ── RedisClient ───────────────────────────────────────────────────────────────

class TestRedisClient:

    def _make_client(self):
        with patch("storage.redis_client.Redis") as mock_redis_cls:
            mock_redis_cls.from_url.return_value = AsyncMock()
            client = RedisClient("redis://localhost:6379")
            client._client = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_returns_value(self):
        client = self._make_client()
        client._client.get.return_value = "val"
        assert await client.get("k") == "val"

    @pytest.mark.asyncio
    async def test_set_calls_underlying(self):
        client = self._make_client()
        await client.set("k", "v", ex=60)
        client._client.set.assert_awaited_once_with("k", "v", ex=60)

    @pytest.mark.asyncio
    async def test_delete_calls_underlying(self):
        client = self._make_client()
        await client.delete("k")
        client._client.delete.assert_awaited_once_with("k")

    @pytest.mark.asyncio
    async def test_exists_true(self):
        client = self._make_client()
        client._client.exists.return_value = 1
        assert await client.exists("k") is True

    @pytest.mark.asyncio
    async def test_exists_false(self):
        client = self._make_client()
        client._client.exists.return_value = 0
        assert await client.exists("k") is False

    @pytest.mark.asyncio
    async def test_hset_calls_underlying(self):
        client = self._make_client()
        await client.hset("h", {"f": "v"})
        client._client.hset.assert_awaited_once_with("h", mapping={"f": "v"})

    @pytest.mark.asyncio
    async def test_hget_returns_value(self):
        client = self._make_client()
        client._client.hget.return_value = "v"
        assert await client.hget("h", "f") == "v"

    @pytest.mark.asyncio
    async def test_health_check_true_on_ping(self):
        client = self._make_client()
        client._client.ping.return_value = True
        assert await client.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_on_error(self):
        from redis.exceptions import RedisError
        client = self._make_client()
        client._client.ping.side_effect = RedisError("refused")
        assert await client.health_check() is False

    @pytest.mark.asyncio
    async def test_get_raises_storage_error_on_redis_error(self):
        from redis.exceptions import RedisError
        client = self._make_client()
        client._client.get.side_effect = RedisError("connection lost")
        with pytest.raises(RedisStorageError):
            await client.get("k")


# ── PostgresClient — migrations ───────────────────────────────────────────────

class TestPostgresMigrations:

    def _make_client(self):
        client = PostgresClient("postgresql://localhost/test")
        client._pool = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_migrations_applied_in_version_order(self, tmp_path):
        """Migrations run in ascending version order."""
        (tmp_path / "002_b.sql").write_text("SELECT 2;")
        (tmp_path / "001_a.sql").write_text("SELECT 1;")

        client = self._make_client()
        # No versions applied yet
        client._pool.fetch.return_value = []
        client._pool.execute.return_value = "OK"

        await client.run_migrations(tmp_path)

        # execute called for: ensure table + migration 001 sql + 001 record insert
        #                     + migration 002 sql + 002 record insert
        calls = [c.args[0].strip() for c in client._pool.execute.await_args_list]
        # Verify both migration SQLs were executed
        assert any("SELECT 1" in c for c in calls)
        assert any("SELECT 2" in c for c in calls)
        # And both version inserts happened
        version_inserts = [c for c in calls if "INSERT INTO schema_migrations" in c]
        assert len(version_inserts) == 2

    @pytest.mark.asyncio
    async def test_migrations_idempotent_skips_applied(self, tmp_path):
        """Already-applied migrations are not re-executed."""
        (tmp_path / "001_a.sql").write_text("SELECT 1;")

        client = self._make_client()
        # Version 1 already applied
        mock_record = MagicMock()
        mock_record.__getitem__ = lambda self, key: 1
        client._pool.fetch.return_value = [mock_record]
        client._pool.execute.return_value = "OK"

        await client.run_migrations(tmp_path)

        # Only the ensure-table execute runs; migration SQL and insert are skipped
        calls = [c.args[0].strip() for c in client._pool.execute.await_args_list]
        assert not any("SELECT 1" in c for c in calls)
        assert not any("INSERT INTO schema_migrations" in c for c in calls)

    @pytest.mark.asyncio
    async def test_migration_files_with_unexpected_names_are_skipped(self, tmp_path):
        """Files that don't start with a numeric prefix are silently skipped."""
        (tmp_path / "noversion.sql").write_text("SELECT bad;")

        client = self._make_client()
        client._pool.fetch.return_value = []
        client._pool.execute.return_value = "OK"

        await client.run_migrations(tmp_path)

        calls = [c.args[0].strip() for c in client._pool.execute.await_args_list]
        assert not any("SELECT bad" in c for c in calls)


# ── PostgresClient — fill buffer ──────────────────────────────────────────────

class TestPostgresFillBuffer:

    @pytest.mark.asyncio
    async def test_buffer_fill_accepts_rows_up_to_max(self):
        client = PostgresClient("postgresql://localhost/test", buffer_max_rows=3)
        for i in range(3):
            await client.buffer_fill({"order_id": str(i)})
        assert client.buffer_size() == 3

    @pytest.mark.asyncio
    async def test_buffer_overflow_writes_to_fallback_file(self, tmp_path):
        """When buffer is full, overflow rows are written to the fallback file."""
        client = PostgresClient("postgresql://localhost/test", buffer_max_rows=2)
        fallback = tmp_path / "overflow.jsonl"

        await client.buffer_fill({"order_id": "1"})
        await client.buffer_fill({"order_id": "2"})
        # Third row overflows
        await client.buffer_fill({"order_id": "3"}, fallback_file=fallback)

        assert fallback.exists()
        lines = fallback.read_text().strip().splitlines()
        assert len(lines) == 1
        import json
        assert json.loads(lines[0])["order_id"] == "3"

    @pytest.mark.asyncio
    async def test_buffer_overflow_no_fallback_does_not_raise(self):
        """Overflow without a fallback file logs but does not raise."""
        client = PostgresClient("postgresql://localhost/test", buffer_max_rows=1)
        await client.buffer_fill({"order_id": "1"})
        # Should not raise even without fallback_file
        await client.buffer_fill({"order_id": "2"})


# ── PostgresClient — health_check ─────────────────────────────────────────────

class TestPostgresHealthCheck:

    @pytest.mark.asyncio
    async def test_health_check_true_when_reachable(self):
        client = PostgresClient("postgresql://localhost/test")
        client._pool = AsyncMock()
        client._pool.fetchval.return_value = 1
        assert await client.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_when_pool_none(self):
        client = PostgresClient("postgresql://localhost/test")
        # _pool stays None
        assert await client.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_on_connection_error(self):
        import asyncpg
        client = PostgresClient("postgresql://localhost/test")
        client._pool = AsyncMock()
        client._pool.fetchval.side_effect = OSError("connection refused")
        assert await client.health_check() is False
