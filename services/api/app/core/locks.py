import secrets

from redis.asyncio import Redis

# Delete the key only if we still own it, so an expired lock taken over by someone else is not released.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisLock:
    """Single-instance Redis lock (SET NX PX + compare-and-delete).

    Good enough for avoiding duplicate work (cache rebuilds, periodic jobs). It is not a fencing
    mechanism: correctness-critical sections rely on database locks instead.
    """

    def __init__(self, redis: Redis, key: str, ttl_ms: int) -> None:
        self._redis = redis
        self.key = key
        self._ttl_ms = ttl_ms
        self._token = secrets.token_hex(16)

    async def acquire(self) -> bool:
        return bool(await self._redis.set(self.key, self._token, nx=True, px=self._ttl_ms))

    async def release(self) -> None:
        await self._redis.eval(_RELEASE_SCRIPT, 1, self.key, self._token)
