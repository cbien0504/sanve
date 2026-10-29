import email
import json
import uuid
from concurrent.futures import ProcessPoolExecutor
from email.message import EmailMessage
from typing import Any

from sqlalchemy import select

from app.domain.outbox import add_outbox_message
from app.integrations.mailer import Mailer
from app.models import EventSalesStats, OutboxMessage
from app.resources import Resources
from app.workers import handlers
from app.workers.relay import relay_batch


class FakeExchange:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, message: Any, routing_key: str, **_: Any) -> None:
        self.published.append((routing_key, message))


async def test_relay_publishes_each_outbox_row_once(resources: Resources) -> None:
    async with resources.sessionmaker() as session, session.begin():
        add_outbox_message(session, "order.paid", "o-1", {"order_id": "o-1"})
        add_outbox_message(session, "order.expired", "o-2", {"order_id": "o-2"})

    exchange = FakeExchange()
    assert await relay_batch(resources.sessionmaker, exchange) == 2  # type: ignore[arg-type]
    assert await relay_batch(resources.sessionmaker, exchange) == 0  # already marked as published

    assert [key for key, _ in exchange.published] == ["order.paid", "order.expired"]
    _, message = exchange.published[0]
    assert json.loads(message.body) == {"order_id": "o-1"}
    async with resources.sessionmaker() as session:
        first_id = await session.scalar(select(OutboxMessage.id).order_by(OutboxMessage.id))
    assert message.message_id == str(first_id)  # consumers deduplicate on this id


async def test_sales_projection_ignores_redelivered_messages(create_event, resources: Resources) -> None:
    event = await create_event()
    payload = {"event_id": event["id"], "quantity": 2, "amount": 1_000_000}

    assert await handlers.update_sales_stats(resources.sessionmaker, "41", "order.paid", payload) == "ok"
    assert (
        await handlers.update_sales_stats(resources.sessionmaker, "41", "order.paid", payload) == "duplicate"
    )
    assert await handlers.update_sales_stats(resources.sessionmaker, "42", "order.paid", payload) == "ok"

    async with resources.sessionmaker() as session:
        row = await session.get(EventSalesStats, uuid.UUID(event["id"]))
    assert row is not None
    assert (row.tickets_sold, row.orders_paid, row.revenue) == (4, 2, 2_000_000)


class CapturingMailer(Mailer):
    def __init__(self, resources: Resources, pool: ProcessPoolExecutor) -> None:
        super().__init__(resources.settings, pool)
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


async def test_ticket_email_has_qr_codes_and_escapes_user_input(resources: Resources) -> None:
    payload = {
        "user_email": "an@example.com",
        "user_name": "An <script>",
        "event_title": "Đêm nhạc <b>Rock</b>",
        "event_starts_at": "2026-12-24T20:00:00+07:00",
        "venue": "Mỹ Đình",
        "amount": 1_000_000,
        "currency": "VND",
        "tickets": [{"seat": "A-001", "code": "code-1"}, {"seat": "A-002", "code": "code-2"}],
    }
    with ProcessPoolExecutor(max_workers=1) as pool:
        mailer = CapturingMailer(resources, pool)
        assert (
            await handlers.send_notification(resources.sessionmaker, mailer, "m-1", "order.paid", payload)
            == "ok"
        )
        again = await handlers.send_notification(resources.sessionmaker, mailer, "m-1", "order.paid", payload)
        assert again == "duplicate"

    assert len(mailer.sent) == 1
    parsed = email.message_from_bytes(bytes(mailer.sent[0]))
    images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
    html = next(part for part in parsed.walk() if part.get_content_type() == "text/html").get_payload(
        decode=True
    )
    assert len(images) == 2
    assert b"<script>" not in html and b"&lt;script&gt;" in html
