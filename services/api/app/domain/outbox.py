"""Reliable messaging helpers: transactional outbox (producer side) and inbox dedupe (consumer side)."""

from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxMessage, ProcessedMessage
from app.observability.tracing import current_trace_carrier


def add_outbox_message(
    session: AsyncSession, event_type: str, aggregate_id: object, payload: dict[str, Any]
) -> None:
    """Record an integration event in the caller's transaction (transactional outbox).

    Writing to RabbitMQ directly from the request would either lose the message (crash after commit)
    or publish a message for a transaction that rolled back. The relay publishes it after commit.
    """
    session.add(
        OutboxMessage(
            event_type=event_type,
            aggregate_id=str(aggregate_id),
            payload=to_jsonable_python(payload),
            trace_context=current_trace_carrier(),
        )
    )


async def mark_processed(session: AsyncSession, consumer: str, message_id: str) -> bool:
    """Returns False when this consumer already handled the message (at-least-once delivery)."""
    result = await session.execute(
        insert(ProcessedMessage)
        .values(consumer=consumer, message_id=message_id)
        .on_conflict_do_nothing()
        .returning(ProcessedMessage.message_id)
    )
    return result.scalar_one_or_none() is not None
