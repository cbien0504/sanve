from dataclasses import dataclass
from typing import Self

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.cache import Cache
from app.core.rate_limit import RateLimiter
from app.db import create_engine, create_sessionmaker
from app.integrations.payment_gateway import PaymentGatewayClient
from app.observability.tracing import instrument_engine


@dataclass
class Resources:
    """Long-lived clients shared by every request of one process (connection pools, HTTP clients)."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    cache: Cache
    rate_limiter: RateLimiter
    payment: PaymentGatewayClient

    @classmethod
    def create(cls, settings: Settings) -> Self:
        engine = create_engine(settings)
        instrument_engine(engine)
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            cache=Cache(redis),
            rate_limiter=RateLimiter(redis),
            payment=PaymentGatewayClient(
                base_url=settings.payment_base_url,
                api_key=settings.payment_api_key,
                timeout=settings.payment_timeout_seconds,
                max_attempts=settings.payment_max_attempts,
                backoff_base=settings.payment_backoff_base_seconds,
                max_concurrency=settings.payment_max_concurrency,
            ),
        )

    async def close(self) -> None:
        await self.payment.aclose()
        await self.redis.aclose()
        await self.engine.dispose()
