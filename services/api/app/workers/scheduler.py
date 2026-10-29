"""Scheduler process: outbox relay + periodic clean-up jobs.

Run with `python -m app.workers.scheduler`. Safe to run several replicas:
- the relay splits work with FOR UPDATE SKIP LOCKED;
- clean-up jobs are also SKIP LOCKED, plus a Redis lock so replicas do not repeat the same sweep.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from prometheus_client import start_http_server
from sqlalchemy import delete

from app.config import get_settings
from app.core.locks import RedisLock
from app.domain import booking
from app.models import IdempotencyKey, OutboxMessage, ProcessedMessage
from app.observability.logging import configure_logging
from app.observability.metrics import JANITOR_ITEMS
from app.observability.tracing import configure_sentry, configure_tracing
from app.resources import Resources
from app.workers.messaging import connect, declare_topology, stop_event
from app.workers.relay import relay_batch, update_outbox_lag

log = structlog.get_logger("sanve.scheduler")


async def run_every(
    name: str, interval: float, job: Callable[[], Awaitable[object]], stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await job()
        except Exception:
            log.exception("job_failed", job=name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)  # interruptible sleep


def singleton_job(resources: Resources, name: str, ttl_ms: int, job: Callable[[], Awaitable[int]]):
    async def run() -> None:
        lock = RedisLock(resources.redis, f"sanve:lock:job:{name}", ttl_ms=ttl_ms)
        if not await lock.acquire():
            return  # another replica is running this sweep
        try:
            count = await job()
            if count:
                JANITOR_ITEMS.labels(name).inc(count)
                log.info("job_done", job=name, items=count)
        finally:
            await lock.release()

    return run


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    configure_sentry(settings)
    if settings.metrics_port:
        start_http_server(settings.metrics_port)

    resources = Resources.create(settings)
    sessionmaker = resources.sessionmaker
    connection = await connect(settings.rabbitmq_url)
    channel = await connection.channel(publisher_confirms=True)
    exchange = await declare_topology(channel)
    stop = stop_event()

    async def relay() -> None:
        # Drain quickly while there is a backlog, then fall back to the polling interval.
        while await relay_batch(sessionmaker, exchange) == 100:
            pass
        await update_outbox_lag(sessionmaker)

    async def expire_holds() -> int:
        async with sessionmaker() as session, session.begin():
            return await booking.expire_holds(session)

    async def expire_orders() -> int:
        async with sessionmaker() as session, session.begin():
            return await booking.expire_orders(session)

    async def purge() -> int:
        now = datetime.now(UTC)
        async with sessionmaker() as session, session.begin():
            results = [
                await session.execute(
                    delete(IdempotencyKey).where(IdempotencyKey.created_at < now - timedelta(days=1))
                ),
                await session.execute(
                    delete(OutboxMessage).where(OutboxMessage.published_at < now - timedelta(days=7))
                ),
                await session.execute(
                    delete(ProcessedMessage).where(ProcessedMessage.processed_at < now - timedelta(days=7))
                ),
            ]
        return sum(result.rowcount for result in results)

    log.info("scheduler_started")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run_every("outbox_relay", 0.5, relay, stop))
        tasks.create_task(
            run_every("expire_holds", 5, singleton_job(resources, "expire_holds", 30_000, expire_holds), stop)
        )
        tasks.create_task(
            run_every(
                "expire_orders", 10, singleton_job(resources, "expire_orders", 30_000, expire_orders), stop
            )
        )
        tasks.create_task(run_every("purge", 3600, singleton_job(resources, "purge", 600_000, purge), stop))

    log.info("scheduler_stopping")
    await connection.close()
    await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
