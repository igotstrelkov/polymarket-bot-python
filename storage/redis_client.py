"""
Async Redis wrapper.

Raises StorageError on RedisError so callers don't depend on redis internals.
"""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a storage operation fails."""


class RedisClient:
    """Thin async wrapper around redis.asyncio.Redis."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        try:
            return await self._client.get(key)
        except RedisError as exc:
            raise StorageError(f"Redis GET {key!r} failed: {exc}") from exc

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        try:
            await self._client.set(key, value, ex=ex)
        except RedisError as exc:
            raise StorageError(f"Redis SET {key!r} failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except RedisError as exc:
            raise StorageError(f"Redis DELETE {key!r} failed: {exc}") from exc

    async def exists(self, key: str) -> bool:
        try:
            return bool(await self._client.exists(key))
        except RedisError as exc:
            raise StorageError(f"Redis EXISTS {key!r} failed: {exc}") from exc

    async def hset(self, name: str, mapping: dict) -> None:
        try:
            await self._client.hset(name, mapping=mapping)
        except RedisError as exc:
            raise StorageError(f"Redis HSET {name!r} failed: {exc}") from exc

    async def hget(self, name: str, key: str) -> str | None:
        try:
            return await self._client.hget(name, key)
        except RedisError as exc:
            raise StorageError(f"Redis HGET {name!r}/{key!r} failed: {exc}") from exc

    async def health_check(self) -> bool:
        try:
            await self._client.ping()
            return True
        except RedisError:
            return False

    async def close(self) -> None:
        await self._client.aclose()
