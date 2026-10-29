"""Queue worker: `python -m app.workers.consumer`. Scale it horizontally by running more replicas."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Any

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from opentelemetry import trace
from prometheus_client import start_http_server

from app.config import get_settings
from app.integrations.mailer import Mailer
from app.observability.logging import configure_logging
from app.observability.metrics import MESSAGES
from app.observability.tracing import configure_sentry, configure_tracing, context_from_carrier
from app.resources import Resources
from app.workers import handlers
from app.workers.messaging import (
    NOTIFICATIONS,
    RETRY_DELAYS_MS,
    SALES_STATS,
    QueueSpec,
    connect,
    declare_topology,
    stop_event,
)

log = structlog.get_logger("sanve.consumer")
tracer = trace.get_tracer("sanve.consumer")

Handler = Callable[[str, str, dict[str, Any]], Awaitable[str]]


class Consumer:
    def __init__(self, channel: AbstractChannel, spec: QueueSpec, handler: Handler, concurrency: int) -> None:
        self._channel = channel
        self._spec = spec
        self._handler = handler
        # Bounded concurrency: at most `concurrency` messages of this queue are processed at once.
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        queue = await self._channel.get_queue(self._spec.name)
        consumer_tag = await queue.consume(self._on_message)
        await stop.wait()
        await queue.cancel(consumer_tag)  # stop receiving; unacked messages go back to the queue
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=25)  # let in-flight messages finish

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        await self._slots.acquire()
        task = asyncio.create_task(self._process(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._slots.release())

    async def _process(self, message: AbstractIncomingMessage) -> None:
        headers = dict(message.headers or {})
        attempt = int(headers.get("x-attempt", 0))  # type: ignore[arg-type]
        message_id = message.message_id or ""
        event_type = message.type or ""
        structlog.contextvars.bind_contextvars(
            queue=self._spec.name, message_id=message_id, event_type=event_type
        )
        # Continue the trace started by the HTTP request that wrote the outbox row.
        with tracer.start_as_current_span(
            f"process {self._spec.name}", context=context_from_carrier(headers), kind=trace.SpanKind.CONSUMER
        ):
            try:
                outcome = await self._handler(message_id, event_type, json.loads(message.body))
            except Exception as exc:
                await self._retry_or_dead_letter(message, headers, attempt, exc)
            else:
                await message.ack()
                MESSAGES.labels(self._spec.name, outcome).inc()

    async def _retry_or_dead_letter(
        self, message: AbstractIncomingMessage, headers: dict[str, Any], attempt: int, exc: Exception
    ) -> None:
        if attempt < len(RETRY_DELAYS_MS):
            target, outcome = self._spec.retry_queue(attempt + 1), "retry"
            log.warning("message_failed_will_retry", attempt=attempt + 1, error=repr(exc))
        else:
            target, outcome = self._spec.dead_letter_queue, "dead_letter"
            log.error("message_dead_lettered", attempts=attempt + 1, error=repr(exc))
        await self._channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers={**headers, "x-attempt": attempt + 1, "x-last-error": repr(exc)[:500]},
                message_id=message.message_id,
                type=message.type,
                content_type=message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=target,
        )
        # Ack only after the copy is safely in the retry/dead-letter queue (publisher confirms).
        await message.ack()
        MESSAGES.labels(self._spec.name, outcome).inc()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    configure_sentry(settings)
    if settings.metrics_port:
        start_http_server(settings.metrics_port)

    resources = Resources.create(settings)
    connection = await connect(settings.rabbitmq_url)
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=20)
    await declare_topology(channel)

    with ProcessPoolExecutor(max_workers=2) as pool:
        mailer = Mailer(settings, pool)
        consumers = [
            Consumer(
                channel,
                NOTIFICATIONS,
                partial(handlers.send_notification, resources.sessionmaker, mailer),
                10,
            ),
            Consumer(channel, SALES_STATS, partial(handlers.update_sales_stats, resources.sessionmaker), 10),
        ]
        stop = stop_event()
        log.info("consumer_started", queues=[spec.name for spec in (NOTIFICATIONS, SALES_STATS)])
        async with asyncio.TaskGroup() as tasks:
            for consumer in consumers:
                tasks.create_task(consumer.run(stop))

    log.info("consumer_stopping")
    await connection.close()
    await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
