import json

import aio_pika
from aio_pika.abc import AbstractExchange
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import OutboxMessage
from app.observability.metrics import OUTBOX_LAG, OUTBOX_PUBLISHED


async def relay_batch(
    sessionmaker: async_sessionmaker[AsyncSession], exchange: AbstractExchange, *, batch: int = 100
) -> int:
    """Publish unpublished outbox rows, oldest first.

    FOR UPDATE SKIP LOCKED lets several relay replicas run side by side without publishing the same
    row twice in the normal case. If we crash after publishing but before committing, the rows are
    published again later: delivery is at-least-once and consumers deduplicate by message_id.
    """
    async with sessionmaker() as session, session.begin():
        messages = (
            await session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.published_at.is_(None))
                .order_by(OutboxMessage.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for message in messages:
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(message.payload).encode(),
                    message_id=str(message.id),
                    type=message.event_type,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    timestamp=message.created_at,
                    headers=dict(message.trace_context),
                ),
                routing_key=message.event_type,
                mandatory=False,  # events nobody subscribes to yet (order.created) are simply dropped
            )
            message.published_at = func.now()
        OUTBOX_PUBLISHED.inc(len(messages))
    return len(messages)


async def update_outbox_lag(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        oldest = await session.scalar(
            select(func.extract("epoch", func.now() - OutboxMessage.created_at))
            .where(OutboxMessage.published_at.is_(None))
            .order_by(OutboxMessage.id)
            .limit(1)
        )
    OUTBOX_LAG.set(float(oldest or 0))
