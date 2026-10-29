"""RabbitMQ topology.

    outbox relay --(routing key = event type)--> exchange "sanve.events" (topic)
        ├── queue "notifications"  (order.paid, order.expired, order.payment_failed, order.refund_pending)
        └── queue "sales-stats"    (order.paid)

A failed message is re-published to "<queue>.retry.<n>": a queue with a TTL whose dead-letter target is
the original queue, i.e. a delayed retry. After the last delay it goes to "<queue>.dlq" for a human.
Retry queues dead-letter through the default exchange straight into *their* queue, so a retry for one
consumer is never delivered again to the other consumers bound to the same routing key.
"""

import asyncio
import contextlib
import signal
from dataclasses import dataclass

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

log = structlog.get_logger(__name__)

EXCHANGE = "sanve.events"
RETRY_DELAYS_MS = (1_000, 5_000, 30_000)


@dataclass(frozen=True)
class QueueSpec:
    name: str
    routing_keys: tuple[str, ...]

    def retry_queue(self, attempt: int) -> str:
        return f"{self.name}.retry.{attempt}"

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.name}.dlq"


NOTIFICATIONS = QueueSpec(
    "notifications", ("order.paid", "order.expired", "order.payment_failed", "order.refund_pending")
)
SALES_STATS = QueueSpec("sales-stats", ("order.paid",))
QUEUES = (NOTIFICATIONS, SALES_STATS)


async def declare_topology(channel: AbstractChannel) -> AbstractExchange:
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    for spec in QUEUES:
        queue = await channel.declare_queue(spec.name, durable=True)
        for routing_key in spec.routing_keys:
            await queue.bind(exchange, routing_key)
        for attempt, delay in enumerate(RETRY_DELAYS_MS, start=1):
            await channel.declare_queue(
                spec.retry_queue(attempt),
                durable=True,
                arguments={
                    "x-message-ttl": delay,
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": spec.name,
                },
            )
        await channel.declare_queue(spec.dead_letter_queue, durable=True)
    return exchange


def stop_event() -> asyncio.Event:
    """Set on SIGTERM/SIGINT so workers finish in-flight work before exiting (graceful shutdown)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop


async def connect(url: str, *, attempts: int = 30, delay: float = 2.0) -> AbstractRobustConnection:
    """connect_robust reconnects after a drop, but the first connection must succeed: retry it so a
    worker that starts before the broker (Kubernetes gives no start order) waits instead of crashing."""
    for attempt in range(1, attempts + 1):
        try:
            return await aio_pika.connect_robust(url)
        except (ConnectionError, aio_pika.exceptions.AMQPConnectionError) as exc:
            if attempt == attempts:
                raise
            log.warning("rabbitmq_unavailable_retrying", attempt=attempt, error=str(exc))
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
