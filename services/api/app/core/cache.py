import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.locks import RedisLock
from app.observability.metrics import CACHE_REQUESTS

log = structlog.get_logger(__name__)

_PREFIX = "sanve:cache"


class Cache:
    """Cache-aside JSON cache.

    - TTL jitter so keys written together do not expire together.
    - Single-flight rebuild: on a miss only one caller per key hits the database, others wait briefly.
    - Fail-open: when Redis is down we serve from the database instead of failing the request.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get_or_load(self, name: str, key: str, ttl: int, loader: Callable[[], Awaitable[Any]]) -> Any:
        full_key = f"{_PREFIX}:{key}"
        cached = await self._get(full_key)
        if cached is not None:
            CACHE_REQUESTS.labels(name, "hit").inc()
            return cached
        CACHE_REQUESTS.labels(name, "miss").inc()

        lock = RedisLock(self._redis, f"{full_key}:rebuild", ttl_ms=5000)
        try:
            is_winner = await lock.acquire()
        except RedisError:
            return await loader()

        if is_winner:
            try:
                value = await loader()
                if value is not None:
                    await self._set(full_key, value, ttl)
                return value
            finally:
                await self._safe_release(lock)

        for _ in range(10):
            await asyncio.sleep(0.05)
            cached = await self._get(full_key)
            if cached is not None:
                return cached
        return await loader()

    async def delete(self, *keys: str) -> None:
        try:
            await self._redis.delete(*(f"{_PREFIX}:{key}" for key in keys))
        except RedisError:
            log.warning("cache_delete_failed", keys=keys)

    async def namespace_version(self, namespace: str) -> int:
        """Versioned namespaces invalidate whole families of keys (e.g. every list page) in O(1)."""
        try:
            value = await self._redis.get(f"{_PREFIX}:{namespace}:version")
        except RedisError:
            return 0
        return int(value or 0)

    async def bump_namespace(self, namespace: str) -> None:
        try:
            await self._redis.incr(f"{_PREFIX}:{namespace}:version")
        except RedisError:
            log.warning("cache_bump_failed", namespace=namespace)

    async def _get(self, key: str) -> Any:
        try:
            raw = await self._redis.get(key)
        except RedisError:
            log.warning("cache_unavailable", key=key)
            return None
        return None if raw is None else json.loads(raw)

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        jittered = ttl + random.randint(0, max(1, ttl // 10))  # noqa: S311 - not security sensitive
        try:
            await self._redis.set(key, json.dumps(value, default=str), ex=jittered)
        except RedisError:
            log.warning("cache_set_failed", key=key)

    async def _safe_release(self, lock: RedisLock) -> None:
        try:
            await lock.release()
        except RedisError:
            log.warning("cache_lock_release_failed", key=lock.key)
