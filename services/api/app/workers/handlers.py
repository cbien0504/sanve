"""Message handlers. Each one is idempotent: the same message may be delivered more than once."""

from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.outbox import mark_processed
from app.integrations.mailer import Mailer
from app.models import EventSalesStats, ProcessedMessage


async def update_sales_stats(
    sessionmaker: async_sessionmaker[AsyncSession], message_id: str, event_type: str, payload: dict[str, Any]
) -> str:
    """Projection update and dedupe marker commit together, so a redelivery can never count twice."""
    async with sessionmaker() as session, session.begin():
        if not await mark_processed(session, "sales-stats", message_id):
            return "duplicate"
        stmt = insert(EventSalesStats).values(
            event_id=payload["event_id"],
            tickets_sold=payload["quantity"],
            orders_paid=1,
            revenue=payload["amount"],
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[EventSalesStats.event_id],
                set_={
                    "tickets_sold": EventSalesStats.tickets_sold + stmt.excluded.tickets_sold,
                    "orders_paid": EventSalesStats.orders_paid + 1,
                    "revenue": EventSalesStats.revenue + stmt.excluded.revenue,
                    "updated_at": func.now(),
                },
            )
        )
    return "ok"


_SUBJECTS = {
    "order.paid": "Vé của bạn cho {event_title}",
    "order.expired": "Đơn hàng {event_title} đã hết hạn thanh toán",
    "order.payment_failed": "Thanh toán cho {event_title} không thành công",
    "order.refund_pending": "SanVe sẽ hoàn tiền đơn hàng {event_title}",
}


async def send_notification(
    sessionmaker: async_sessionmaker[AsyncSession],
    mailer: Mailer,
    message_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Email side effects cannot share a transaction with the database. We check, send, then mark:
    a crash between "send" and "mark" means one duplicate email, which is the accepted trade-off."""
    async with sessionmaker() as session:
        already = await session.get(ProcessedMessage, ("notifications", message_id))
    if already is not None:
        return "duplicate"
    if event_type not in _SUBJECTS:
        return "ignored"

    email = mailer.new_message(payload["user_email"], _SUBJECTS[event_type].format(**payload))
    email.set_content(_plain_text(event_type, payload))
    if event_type == "order.paid":
        tickets = payload["tickets"]
        images = await mailer.qr_codes([ticket["code"] for ticket in tickets])
        email.add_alternative(_tickets_html(payload, len(images)), subtype="html")
        html_part = email.get_payload()[-1]
        for index, image in enumerate(images):
            html_part.add_related(image, "image", "png", cid=f"<qr{index}@sanve.local>")
    await mailer.send(email)

    async with sessionmaker() as session, session.begin():
        await mark_processed(session, "notifications", message_id)
    return "ok"


def _plain_text(event_type: str, payload: dict[str, Any]) -> str:
    starts_at = datetime.fromisoformat(payload["event_starts_at"]).strftime("%H:%M %d/%m/%Y")
    lines = [f"Xin chào {payload['user_name']},", ""]
    if event_type == "order.paid":
        lines.append(
            f"Thanh toán thành công {payload['amount']:,} {payload['currency']} cho {payload['event_title']}."
        )
        lines.append(f"Thời gian: {starts_at} tại {payload['venue']}.")
        lines += [f"- Ghế {ticket['seat']}: mã vé {ticket['code']}" for ticket in payload["tickets"]]
    elif event_type == "order.expired":
        lines.append("Đơn hàng đã quá thời gian thanh toán nên ghế đã được mở bán lại.")
    elif event_type == "order.payment_failed":
        lines.append("Cổng thanh toán báo giao dịch không thành công. Ghế đã được mở bán lại.")
    else:
        lines.append(
            "Bạn đã thanh toán sau khi đơn hàng hết hạn. SanVe sẽ hoàn tiền trong 3-5 ngày làm việc."
        )
    return "\n".join(lines)


def _tickets_html(payload: dict[str, Any], count: int) -> str:
    # Names and titles are user input: escape them, or an event title becomes HTML in every inbox.
    rows = "".join(
        f"<li>Ghế <b>{escape(ticket['seat'])}</b> — mã <code>{escape(ticket['code'])}</code><br>"
        f'<img src="cid:qr{index}@sanve.local" width="160" alt="QR"></li>'
        for index, ticket in enumerate(payload["tickets"][:count])
    )
    return (
        f"<p>Xin chào {escape(payload['user_name'])},</p>"
        f"<p>Thanh toán thành công <b>{payload['amount']:,} {escape(payload['currency'])}</b> "
        f"cho <b>{escape(payload['event_title'])}</b> tại {escape(payload['venue'])}.</p>"
        f"<ul>{rows}</ul><p>Đưa mã QR cho nhân viên soát vé khi vào cổng.</p>"
    )
