"""Seat holds, orders and payments: the part of the system that must never sell a seat twice."""

import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.events import sale_is_open
from app.domain.outbox import add_outbox_message, mark_processed
from app.domain.state_machine import HOLD_FLOW, ORDER_FLOW
from app.errors import BadGateway, Conflict, NotFound, ServiceUnavailable
from app.integrations.payment_gateway import PaymentGatewayError, PaymentGatewayUnavailable
from app.models import (
    Event,
    EventStatus,
    Hold,
    HoldStatus,
    Order,
    OrderStatus,
    Seat,
    SeatStatus,
    Ticket,
    TicketTier,
    User,
)
from app.observability.metrics import HOLDS, ORDER_TRANSITIONS
from app.resources import Resources
from app.schemas import HoldCreate, HoldOut, PaymentWebhook
from app.security import CurrentUser

log = structlog.get_logger(__name__)

# --- holds ------------------------------------------------------------------------------------------


async def create_hold(
    session: AsyncSession, user: CurrentUser, data: HoldCreate, settings: Settings
) -> HoldOut:
    now = datetime.now(UTC)
    event = await session.get(Event, data.event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        raise NotFound("Event not found")
    if not sale_is_open(event, now):
        raise Conflict("Ticket sale is not open for this event", code="sale_not_open")
    tier = await session.get(TicketTier, data.tier_id)
    if tier is None or tier.event_id != event.id:
        raise NotFound("Ticket tier not found")

    # Parallel requests of the same buyer for the same event queue up here (transaction-scoped
    # advisory lock), so the per-buyer cap below cannot be bypassed by firing requests in parallel.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"hold:{user.id}:{event.id}", 0)))
    )
    reserved = await _tickets_in_flight(session, user.id, event.id, now)
    if reserved + data.quantity > event.max_tickets_per_user:
        HOLDS.labels("limit_exceeded").inc()
        raise Conflict(
            f"You can buy at most {event.max_tickets_per_user} tickets for this event",
            code="ticket_limit_exceeded",
            extra={"already_reserved": reserved},
        )

    # FOR UPDATE SKIP LOCKED: concurrent buyers lock *different* free seats instead of all waiting
    # on the first free row. The partial index (tier_id, id) WHERE status = 'available' serves it.
    seats = (
        await session.execute(
            select(Seat.id, Seat.label)
            .where(Seat.tier_id == tier.id, Seat.status == SeatStatus.AVAILABLE)
            .order_by(Seat.id)
            .limit(data.quantity)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if len(seats) < data.quantity:
        HOLDS.labels("sold_out").inc()
        raise Conflict("Not enough seats left in this tier", code="sold_out")

    hold = Hold(
        user_id=user.id,
        event_id=event.id,
        tier_id=tier.id,
        quantity=data.quantity,
        status=HoldStatus.ACTIVE,
        expires_at=now + timedelta(seconds=settings.hold_ttl_seconds),
    )
    session.add(hold)
    await session.flush()
    await session.execute(
        update(Seat)
        .where(Seat.id.in_([seat.id for seat in seats]))
        .values(status=SeatStatus.HELD, hold_id=hold.id)
    )
    HOLDS.labels("created").inc()
    return _hold_out(hold, [seat.label for seat in seats])


async def get_hold(session: AsyncSession, user: CurrentUser, hold_id: uuid.UUID) -> HoldOut:
    hold = await session.get(Hold, hold_id)
    if hold is None or hold.user_id != user.id:
        raise NotFound("Hold not found")
    labels = await session.scalars(select(Seat.label).where(Seat.hold_id == hold.id).order_by(Seat.id))
    return _hold_out(hold, list(labels))


async def release_hold(session: AsyncSession, user: CurrentUser, hold_id: uuid.UUID) -> None:
    hold = await session.scalar(select(Hold).where(Hold.id == hold_id).with_for_update())
    if hold is None or hold.user_id != user.id:
        raise NotFound("Hold not found")
    if not HOLD_FLOW.can(hold.status, HoldStatus.RELEASED):
        raise Conflict("Hold is no longer active", code="hold_not_active")
    hold.status = HoldStatus.RELEASED
    await release_seats(session, [hold.id])


async def release_seats(session: AsyncSession, hold_ids: Sequence[uuid.UUID]) -> int:
    if not hold_ids:
        return 0
    result = await session.execute(
        update(Seat)
        .where(Seat.hold_id.in_(hold_ids), Seat.status == SeatStatus.HELD)
        .values(status=SeatStatus.AVAILABLE, hold_id=None)
    )
    return result.rowcount


async def _tickets_in_flight(
    session: AsyncSession, user_id: uuid.UUID, event_id: uuid.UUID, now: datetime
) -> int:
    active_holds = (
        select(func.coalesce(func.sum(Hold.quantity), 0))
        .where(
            Hold.user_id == user_id,
            Hold.event_id == event_id,
            Hold.status == HoldStatus.ACTIVE,
            Hold.expires_at > now,
        )
        .scalar_subquery()
    )
    open_orders = (
        select(func.coalesce(func.sum(Order.quantity), 0))
        .where(
            Order.user_id == user_id,
            Order.event_id == event_id,
            Order.status.in_([OrderStatus.PENDING_PAYMENT, OrderStatus.PAID]),
        )
        .scalar_subquery()
    )
    return int(await session.scalar(select(active_holds + open_orders)) or 0)


def _hold_out(hold: Hold, seats: list[str]) -> HoldOut:
    return HoldOut(
        id=hold.id,
        event_id=hold.event_id,
        tier_id=hold.tier_id,
        quantity=hold.quantity,
        status=hold.status,
        seats=seats,
        expires_at=hold.expires_at,
    )


# --- orders -----------------------------------------------------------------------------------------


async def create_order(resources: Resources, user: CurrentUser, hold_id: uuid.UUID) -> tuple[Order, bool]:
    """Turn a hold into an order, then make sure it has a payment intent.

    Three short steps instead of one long transaction, so no row lock is held while we wait on
    the network: (1) create the order, commit; (2) call the gateway; (3) save the intent.
    The call is safe to repeat: one order per hold (unique constraint) and the gateway dedupes
    by Idempotency-Key = order id. A client that got a 503 simply retries the same request.
    """
    settings = resources.settings
    created = False
    async with resources.sessionmaker() as session, session.begin():
        order = await session.scalar(select(Order).where(Order.hold_id == hold_id))
        if order is None:
            hold = await session.scalar(select(Hold).where(Hold.id == hold_id).with_for_update())
            if hold is None or hold.user_id != user.id:
                raise NotFound("Hold not found")
            # Re-check under the lock: a parallel request may have converted the hold meanwhile.
            order = await session.scalar(select(Order).where(Order.hold_id == hold_id))
            if order is None:
                if hold.status != HoldStatus.ACTIVE or hold.expires_at <= datetime.now(UTC):
                    raise Conflict("Hold has expired or was released", code="hold_not_active")
                tier = await session.get(TicketTier, hold.tier_id)
                assert tier is not None
                HOLD_FLOW.ensure(hold.status, HoldStatus.CONVERTED)
                hold.status = HoldStatus.CONVERTED
                order = Order(
                    user_id=user.id,
                    event_id=hold.event_id,
                    hold_id=hold.id,
                    status=OrderStatus.PENDING_PAYMENT,
                    quantity=hold.quantity,
                    amount=tier.price * hold.quantity,
                    currency="VND",
                    expires_at=datetime.now(UTC) + timedelta(seconds=settings.order_ttl_seconds),
                )
                session.add(order)
                await session.flush()
                add_outbox_message(
                    session,
                    "order.created",
                    order.id,
                    {"order_id": order.id, "event_id": order.event_id, "amount": order.amount},
                )
                created = True
        if order.user_id != user.id:
            raise NotFound("Hold not found")

    if order.status != OrderStatus.PENDING_PAYMENT or order.payment_intent_id is not None:
        return order, created

    try:
        intent = await resources.payment.create_intent(
            order_id=order.id,
            amount=order.amount,
            currency=order.currency,
            description=f"SanVe order {order.id}",
        )
    except PaymentGatewayUnavailable as exc:
        raise ServiceUnavailable(
            "Payment gateway is unavailable, please retry",
            code="payment_unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    except PaymentGatewayError as exc:
        raise BadGateway(str(exc)) from exc

    async with resources.sessionmaker() as session, session.begin():
        await session.execute(
            update(Order)
            .where(Order.id == order.id, Order.payment_intent_id.is_(None))
            .values(payment_intent_id=intent.id, checkout_url=intent.checkout_url)
        )
        refreshed = await session.get(Order, order.id)
        assert refreshed is not None
    return refreshed, created


async def get_order(
    session: AsyncSession, user: CurrentUser, order_id: uuid.UUID, *, lock: bool = False
) -> Order:
    stmt = select(Order).where(Order.id == order_id)
    order = await session.scalar(stmt.with_for_update() if lock else stmt)
    # Object-level authorization: other users' orders are indistinguishable from missing ones.
    if order is None or (order.user_id != user.id and user.role != "admin"):
        raise NotFound("Order not found")
    return order


async def cancel_order(session: AsyncSession, user: CurrentUser, order_id: uuid.UUID) -> Order:
    order = await get_order(session, user, order_id, lock=True)
    if not ORDER_FLOW.can(order.status, OrderStatus.CANCELLED):
        raise Conflict("Only orders waiting for payment can be cancelled", code="order_not_cancellable")
    _transition(order, OrderStatus.CANCELLED)
    await release_seats(session, [order.hold_id])
    add_outbox_message(session, "order.cancelled", order.id, await _order_message(session, order))
    return order


def _transition(order: Order, target: OrderStatus) -> None:
    ORDER_FLOW.ensure(order.status, target)
    order.status = target
    ORDER_TRANSITIONS.labels(target.value).inc()


async def _order_message(
    session: AsyncSession, order: Order, *, tickets: list[Ticket] | None = None
) -> dict[str, Any]:
    """Event-carried state: consumers get everything they need without querying our database."""
    user = await session.get(User, order.user_id)
    event = await session.get(Event, order.event_id)
    assert user is not None and event is not None
    message: dict[str, Any] = {
        "order_id": order.id,
        "status": order.status,
        "user_id": user.id,
        "user_email": user.email,
        "user_name": user.full_name,
        "event_id": event.id,
        "event_title": event.title,
        "event_starts_at": event.starts_at,
        "venue": event.venue,
        "quantity": order.quantity,
        "amount": order.amount,
        "currency": order.currency,
    }
    if tickets is not None:
        message["tickets"] = [{"code": ticket.code, "seat": ticket.seat_label} for ticket in tickets]
    return message


# --- payment webhooks -------------------------------------------------------------------------------


async def handle_payment_event(session: AsyncSession, event: PaymentWebhook) -> str:
    """Apply a payment result. Gateways deliver at least once, so this must be idempotent."""
    if not await mark_processed(session, "payment_webhook", event.id):
        return "duplicate"

    order = await session.scalar(select(Order).where(Order.id == event.data.merchant_ref).with_for_update())
    if order is None:
        log.warning("payment_for_unknown_order", merchant_ref=str(event.data.merchant_ref))
        return "unknown_order"
    if (order.amount, order.currency) != (event.data.amount, event.data.currency):
        log.error(
            "payment_amount_mismatch", order_id=str(order.id), expected=order.amount, got=event.data.amount
        )
        return "amount_mismatch"
    if order.payment_intent_id not in (None, event.data.payment_intent_id):
        log.error("payment_intent_mismatch", order_id=str(order.id))
        return "intent_mismatch"
    order.payment_intent_id = event.data.payment_intent_id

    if event.type == "payment.succeeded":
        if order.status == OrderStatus.PENDING_PAYMENT:
            await _mark_paid(session, order)
            return "paid"
        if ORDER_FLOW.can(order.status, OrderStatus.REFUND_PENDING):
            # Late payment: the order already expired and its seats may belong to someone else now.
            _transition(order, OrderStatus.REFUND_PENDING)
            add_outbox_message(
                session, "order.refund_pending", order.id, await _order_message(session, order)
            )
            return "refund_pending"
        return "ignored"

    if order.status == OrderStatus.PENDING_PAYMENT:
        _transition(order, OrderStatus.FAILED)
        await release_seats(session, [order.hold_id])
        add_outbox_message(session, "order.payment_failed", order.id, await _order_message(session, order))
        return "failed"
    return "ignored"


async def _mark_paid(session: AsyncSession, order: Order) -> None:
    _transition(order, OrderStatus.PAID)
    order.paid_at = datetime.now(UTC)
    seats = (
        await session.execute(
            select(Seat.id, Seat.label)
            .where(Seat.hold_id == order.hold_id)
            .order_by(Seat.id)
            .with_for_update()
        )
    ).all()
    if len(seats) != order.quantity:
        log.error(
            "paid_order_seat_mismatch", order_id=str(order.id), seats=len(seats), quantity=order.quantity
        )
    await session.execute(
        update(Seat).where(Seat.id.in_([seat.id for seat in seats])).values(status=SeatStatus.SOLD)
    )
    tickets = [
        Ticket(
            order_id=order.id,
            event_id=order.event_id,
            user_id=order.user_id,
            seat_id=seat.id,
            seat_label=seat.label,
            code=secrets.token_urlsafe(12),
        )
        for seat in seats
    ]
    session.add_all(tickets)
    add_outbox_message(session, "order.paid", order.id, await _order_message(session, order, tickets=tickets))


# --- periodic clean-up (run by the scheduler) -------------------------------------------------------


async def expire_holds(session: AsyncSession, *, batch: int = 500) -> int:
    # SKIP LOCKED lets several scheduler replicas share the work and never blocks a buyer who is
    # converting that very hold into an order right now.
    hold_ids = (
        (
            await session.execute(
                text(
                    """
                WITH expired AS (
                    SELECT id FROM holds
                    WHERE status = 'active' AND expires_at < now()
                    ORDER BY expires_at
                    LIMIT :batch
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE holds h SET status = 'expired'
                FROM expired
                WHERE h.id = expired.id
                RETURNING h.id
                """
                ),
                {"batch": batch},
            )
        )
        .scalars()
        .all()
    )
    await release_seats(session, hold_ids)
    return len(hold_ids)


async def expire_orders(session: AsyncSession, *, batch: int = 200) -> int:
    orders = (
        await session.scalars(
            select(Order)
            .where(Order.status == OrderStatus.PENDING_PAYMENT, Order.expires_at < func.now())
            .order_by(Order.expires_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for order in orders:
        _transition(order, OrderStatus.EXPIRED)
        add_outbox_message(session, "order.expired", order.id, await _order_message(session, order))
    await release_seats(session, [order.hold_id for order in orders])
    return len(orders)
