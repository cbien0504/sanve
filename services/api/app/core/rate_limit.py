from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

# Token bucket evaluated atomically inside Redis. Using the Redis clock (TIME) keeps every API replica
# on the same time source, so clock skew between pods cannot be abused.
_RATE_LIMIT_SCRIPT = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    retry_after = (cost - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 1)
return {allowed, tostring(tokens), tostring(retry_after)}
"""


@dataclass(frozen=True)
class RateLimit:
    name: str
    capacity: int
    per_seconds: float

    @property
    def refill_rate(self) -> float:
        return self.capacity / self.per_seconds


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(_RATE_LIMIT_SCRIPT)

    async def hit(self, limit: RateLimit, identity: str, cost: int = 1) -> RateLimitResult:
        try:
            allowed, tokens, retry_after = await self._script(
                keys=[f"sanve:ratelimit:{limit.name}:{identity}"],
                args=[limit.capacity, limit.refill_rate, cost],
            )
        except RedisError:
            # Fail open: losing rate limiting for a moment is better than rejecting every request.
            log.warning("rate_limiter_unavailable", limit=limit.name)
            return RateLimitResult(True, limit.capacity, limit.capacity, 0.0)
        return RateLimitResult(bool(allowed), limit.capacity, int(float(tokens)), float(retry_after))
